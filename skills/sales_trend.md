---
name: sales_trend
description: 销售趋势分析——月度/周度趋势、环比、区域与产品拆分、季节性判断。全程只调用 mcp__mysql__ 只读工具，把聚合算在 MySQL 侧，结论结构化输出。
---

# 销售趋势分析 Playbook

## 0. 前置
- 数据源：`analytics.sales`（order_date / region / product / quantity / amount / customer_id）
- 先 `mcp__mysql__get_schema_info` 确认表结构，再动手写 SQL
- 时间窗口：默认最近 6 个月（本数据集 2025-12-01 ~ 2026-05-31）；用户给区间则按其口径
- 全程只读：只用 `mcp__mysql__execute_sql` 发 **SELECT**；禁止任何写操作

## 1. 分析步骤（按序执行，每步一条 SQL，聚合在 MySQL 侧，严禁拉全表到本地再算）
1. **总体趋势**：按月聚合金额/订单数，判断方向与幅度
   ```sql
   SELECT DATE_FORMAT(order_date, '%Y-%m') AS ym,
          SUM(amount)                       AS revenue,
          COUNT(*)                          AS orders
   FROM sales
   WHERE order_date BETWEEN '2025-12-01' AND '2026-05-31'
   GROUP BY ym ORDER BY ym;
   ```
2. **环比**：用上一步结果算 month-over-month；对比首尾月得整体涨幅
3. **区域拆分**：`GROUP BY region` 看哪个区域是增长引擎
4. **产品拆分**：`GROUP BY product` 看产品结构变化（低价走量 vs 高价走额）
5. **周季节性**：按星期几聚合，识别周期规律（用于校准，不当作趋势）
   ```sql
   SELECT DAYOFWEEK(order_date) AS dow, AVG(amount) AS avg_revenue
   FROM sales GROUP BY dow ORDER BY dow;
   ```
6. **异常核对**：趋势中若有突兀的尖峰/低谷，交叉引用 anomaly_detect，不自行臆断原因

## 2. SQL 纪律
- 只读白名单语句（SELECT 开头），不加分号多语句
- 金额字段名若带反引号需整表名校验；表名/列名只允许字母数字下划线
- 结果超 1000 行会被截断（服务端护栏），聚合查询通常远小于此
- 返回 JSON 里 `truncated: true` 时要提示用户结果不完整

## 3. 结论结构（固定四段，缺一不可）
1. **趋势判断**：上升/下降/平稳 + 量化幅度（如「6 个月累计增长 ~70%，其中 3 月环比 +35%」）
2. **驱动因素**：哪个区域、哪个产品贡献最大（用数字说话）
3. **季节性特征**：一周内哪天强/弱
4. **风险提示**：异常点、数据口径限制（如窗口外无数据无法同比）

## 4. 质量自检
- 数字必须来自工具返回结果，禁止编造；SQL 报错时换写法重试（错误回喂后调整）
- 结论里每个数字都能回溯到一条已执行的 SQL
