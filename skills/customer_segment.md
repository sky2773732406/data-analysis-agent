---
name: customer_segment
description: 客户分层（RFM 模型）——按最近消费/频次/金额给客户分档分层，输出各层规模与运营建议。只输出 customer_id 与分层标签，绝不输出 name/phone 等 PII。
---

# 客户分层（RFM）Playbook

## 0. 前置与安全红线
- 数据源：`analytics.sales`（客户行为）+ `analytics.customers`（仅用于确认表结构）
- **PII 红线：customers.name / phone 是 PII，任何输出（含中间 SQL 结果）不得包含原始值**；
  需要示例名单时使用脱敏形式（如 `张*`、`138****0001`，脱敏规则见 PII 中间件文档）
- 观察窗口：默认 2025-12-01 ~ 2026-05-31（与数据集一致；评估可复现）
- 参考日：用数据集最新订单日 `MAX(order_date)` 而非 `CURDATE()`（确定性，评估可复现）

## 1. RFM 定义（全部从 sales 按客户聚合，聚合算在 MySQL 侧）
| 维度 | 含义 | 计算 |
| --- | --- | --- |
| R | 距最近一单天数 | `DATEDIFF((SELECT MAX(order_date) FROM sales), MAX(order_date))` |
| F | 订单频次 | `COUNT(*)` |
| M | 累计金额 | `SUM(amount)` |

## 2. 分析步骤
1. **RFM 聚合**：
   ```sql
   SELECT customer_id,
          DATEDIFF((SELECT MAX(order_date) FROM sales), MAX(order_date)) AS r_days,
          COUNT(*)                       AS f_cnt,
          SUM(amount)                    AS m_amt
   FROM sales
   WHERE order_date BETWEEN '2025-12-01' AND '2026-05-31'
   GROUP BY customer_id;
   ```
2. **打分**：R/F/M 三维**统一用样本三分位** NTILE(3)（口径一致；教训：模板固定阈值如 33/66 天是拍脑袋，样本真实分位会不同——本数据集 r_days 的 33/66 分位是 8/29 天）：
   ```sql
   SELECT customer_id, r_days, f_cnt, m_amt,
          NTILE(3) OVER (ORDER BY r_days DESC) AS r_score,  -- 距今天数越少 = 越近 = 高分
          NTILE(3) OVER (ORDER BY f_cnt)       AS f_score,
          NTILE(3) OVER (ORDER BY m_amt)       AS m_score
   FROM (…上一步结果…) t;
   ```
3. **分层映射**（按业务优先级给标签，不必穷举 27 格）：
   - `r=3 AND f=3 AND m=3` → **重要价值客户**（近且高频高额，重点维护）
   - `r>=2 AND f>=2 AND m=3` → **重点发展客户**（潜力大，推升级）
   - `r=1 AND m>=2` → **流失预警**（近期沉默但历史高价值，挽回优先）
   - `r=1 AND m=1` → **低价值流失**（观察即可，勿投资源）
   - 其余 → **一般客户**（常规运营）
4. **沉默客户单列**：customers 表里从未出现在 sales 的客户（`LEFT JOIN … WHERE sales.id IS NULL`）→ 全部计入「沉默/未转化」，单独计数——本数据集有 14 个
5. **汇总输出**：各层客户数 / 客户占比 / 金额贡献占比（m 合计 / 总 m），找出「20% 客户贡献 ~80% 金额」式的头部集中度

## 3. 结论结构
1. 各层规模与金额贡献表（数字来自 SQL）
2. 头部集中度结论（如「top 20% 客户贡献 X% 营收」）
3. 运营建议：每层一句话动作（按业务优先级，不堆砌）

## 4. 质量自检
- 输出不含任何原始 name/phone（违反即视为失败）
- 每层数字可回溯到一条已执行 SQL；分档阈值（33/66 分位）在结论中注明口径
