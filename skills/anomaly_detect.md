---
name: anomaly_detect
description: 异常检测——对日/周粒度销售序列检测尖峰与低谷，量化偏离度并定位区域，不下因果结论。全程只调用 mcp__mysql__ 只读工具。
---

# 异常检测 Playbook

## 0. 前置
- 数据源：`analytics.sales`（order_date / region / amount）
- 目标：找出序列中**统计意义上突兀**的点（尖峰/低谷），量化偏离幅度，**定位到区域**
- 边界声明：本 skill 只报"异常 + 偏离度"，不臆断原因（促销/故障/口径问题由业务确认，或与 sales_trend 交叉）

## 1. 检测方法（**以方法 B 周同比为准**；方法 A 仅作候选池，必须经星期复核）

**方法 A：窗口均值偏离（有已知盲区，仅作候选）**
> 实测教训（本项目）：7 日滑动均值不区分星期几，周末/周一低谷会系统性假阳性
> （一次运行标记 59 天，绝大多数是 −94% 级的星期低谷，如 12-15(周一)、1-20(周一)）。
> 因此方法 A 的标记**不得直接当作结论**——所有候选必须按「同星期几窗口均值」复核，
> 能由星期模式解释的（周一低谷/周末回落）一律排除。
1. 先算每日总金额序列：
   ```sql
   SELECT order_date AS d, SUM(amount) AS revenue
   FROM sales
   WHERE order_date BETWEEN '2025-12-01' AND '2026-05-31'
   GROUP BY order_date ORDER BY d;
   ```
2. 加 7 日居中滑动均值，算偏离百分比（MySQL 8 窗口函数）：
   ```sql
   SELECT d, revenue, DAYOFWEEK(d) AS dow,
          AVG(revenue) OVER (ORDER BY d ROWS BETWEEN 6 PRECEDING AND 6 FOLLOWING) AS ma7,
          revenue / NULLIF(AVG(revenue) OVER (ORDER BY d ROWS BETWEEN 6 PRECEDING AND 6 FOLLOWING), 0) - 1 AS dev
   FROM (…每日序列…) t;
   ```
3. 候选规则：`dev >= +0.8` → 尖峰候选；`dev <= -0.5` → 低谷候选；**再按 dow 分组看同星期均值**，剔除星期模式可解释项

**方法 B：周同比（吸收星期季节性，判定基准）**
```sql
SELECT t1.d, t1.revenue, t0.revenue AS last_week,
       t1.revenue / NULLIF(t0.revenue, 0) - 1 AS wow_dev
FROM (每日序列) t1
JOIN (每日序列) t0 ON t0.d = DATE_SUB(t1.d, INTERVAL 7 DAY);
```
判定：`wow_dev >= +0.8` 或 `<= -0.5`（阈值与本数据集注入信号对齐——种子 v5 实测：华南大促 ×3 → 周级 +270%、华北故障 ×0.05 → 周级 -49% + 日级零单空窗；周级 ±50% 以上才值得进结论）

## 2. 定位到区域
对异常日窗口内的订单做 `GROUP BY region`，确认是否单区域驱动（本数据集已知：华南 3/10-14、华北 4/20-24），还是全盘波动。

## 3. 输出格式
| 日期 | 区域 | 偏离度 | 类型 | 说明 |
| --- | --- | --- | --- | --- |
| 2026-03-10 ~ 03-14 | 华南 | +270%（周级） | 尖峰 | 疑似促销/集中采购，待业务确认 |

## 4. 质量自检
- 所有偏离度来自 SQL 计算结果，禁止编造
- 同时报告"未检出异常"的空结果也是有效结论（数据平稳时如实说）
- 边界日（窗口首尾 3 天内）滑动均值不完整，注明后谨慎下结论
