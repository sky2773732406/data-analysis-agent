"""生成销售种子数据 v5（确定性随机、幂等、可重复执行）。

v2 变更（对应 RFM 分层与 PII 脱敏需求）：
  - 新增 customers 表（name/phone 为 PII，供 PII 脱敏演示）
  - sales 增加 customer_id（RFM 的 R/F/M 全部从 sales 按客户聚合而来）
  - 客户活跃度用 Pareto 权重（少数高活客户 + 长尾）→ RFM 分段非平凡

数据信号（对齐三个 skill 的评估场景）：
  - 增长趋势：6 个月线性 +60%（sales_trend）
  - 周季节性：周三~五微高，周末回落（趋势校准，但幅度温和不掩盖异常）
  - 注入异常：华南 3/10-14 促销尖峰 ×3、华北 4/20-24 故障低谷 ×0.05（anomaly_detect）
  - 客户分层：~200 客户，活跃度 Pareto 分布（customer_segment / RFM）

v4/v5 修复（信噪比，依据 tests/poc/check_anomaly_signal.py 实测）：
  - v3（815 单）过稀：每区域日均仅 1.1 单 → 48% 日子零单，周环比噪声 ±500%+，信号被淹
  - v4：基线日均 4.5 单、qty 收窄到 1-12、低谷加强 ×0.05、星期因子抹平 → 低谷检出（百分位 1%）；
    但几何分布 σ≈μ 仍让尖峰日剧烈抖动（μ≈13 的日子实测 5/0/17/1/3），华南仅 59 百分位
  - v5：订单数改用 Poisson（σ=√μ）→ 日单量稳定，尖峰周应稳定 ≥95 百分位

运行（root 写入；mcp_reader 只读不能造数）：
    conda activate data-analysis-agent
    python scripts/seed_sales.py

幂等：TRUNCATE customers/sales 后批量参数化插入（executemany）。
"""
from __future__ import annotations

import argparse
import getpass
import random
from datetime import date, timedelta

import pymysql
import numpy as np


PRODUCTS = {"标准版": 2000, "高级版": 3000, "企业版": 6000, "旗舰版": 12000, "增值服务": 800}
REGIONS = ["华东", "华南", "华北", "西南"]
REGION_WEIGHTS = [0.40, 0.25, 0.20, 0.15]
WEEKDAY_FACTOR = {0: 0.85, 1: 0.95, 2: 1.0, 3: 1.1, 4: 1.15, 5: 1.0, 6: 0.85}
ANOMALIES = [  # (start, end, factor, region)；v4：低谷加强到 ×0.05（零单空窗更醒目）
    (date(2026, 3, 10), date(2026, 3, 14), 3.0, "华南"),
    (date(2026, 4, 20), date(2026, 4, 24), 0.05, "华北"),
]
SURNAMES = "张李王刘陈杨赵黄周吴徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
GIVEN = "伟芳娜敏静丽强磊军洋勇艳杰娟涛明超秀霞平刚桂英华建辉志文飞欣梅兰玉萍红"


def gen_customers(rng: random.Random, n: int = 200) -> list[dict]:
    """生成客户（确定性）。name/phone 为 PII，活跃度权重 Pareto 分布。"""
    customers = []
    start, end = date(2025, 6, 1), date(2026, 5, 31)
    span = (end - start).days
    for i in range(1, n + 1):
        name = rng.choice(SURNAMES) + rng.choice(GIVEN) + rng.choice(GIVEN)
        phone = f"13{rng.randint(5, 9)}{rng.randint(0, 99999999):08d}"
        region = rng.choices(REGIONS, weights=REGION_WEIGHTS)[0]
        created = start + timedelta(days=rng.randint(0, span))
        weight = rng.paretovariate(1.5)  # 长尾活跃度：少数客户贡献多数订单
        customers.append({"id": i, "name": name, "phone": phone,
                          "region": region, "created_at": created, "w": weight})
    return customers


def main() -> None:
    ap = argparse.ArgumentParser(description="seed customers + sales")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3306)
    ap.add_argument("--db", default="analytics")
    ap.add_argument("--customers", type=int, default=200)
    args = ap.parse_args()

    rng = random.Random(42)
    pois = np.random.default_rng(42)  # Poisson 独立随机流（确定性）
    customers = gen_customers(rng, args.customers)
    by_region: dict[str, list[dict]] = {}
    for c in customers:
        by_region.setdefault(c["region"], []).append(c)

    # 订单生成：与 v1 同构（趋势 × 季节性 × 异常），客户按区域 + Pareto 权重抽取
    start, end = date(2025, 12, 1), date(2026, 5, 31)
    sales_rows: list[tuple] = []
    day = start
    while day <= end:
        growth = 0.7 + 0.6 * ((day - start).days / (end - start).days)
        for region in REGIONS:
            factor = growth * WEEKDAY_FACTOR[day.weekday()]
            for a_start, a_end, a_factor, a_region in ANOMALIES:
                if a_start <= day <= a_end and region == a_region:
                    factor *= a_factor
            # 订单数：Poisson(μ)，σ=√μ —— 日单量稳定，注入信号不被分布抖动吃掉。
            # v5 修复：v4 几何分布 σ≈μ，华南尖峰日(μ≈13)实测 5/0/17/1/3，一半日子掉回基线 →
            # 周环比 +22% 仅 59 百分位不可检出。Poisson 下 μ=13 时 σ≈3.7，日单量稳定。
            n = int(pois.poisson(4.5 * factor))
            pool = by_region[region]
            for _ in range(n):
                cust = rng.choices(pool, weights=[c["w"] for c in pool])[0]
                product = rng.choices(list(PRODUCTS), weights=[4, 3, 2, 1, 2])[0]
                qty = rng.randint(1, 12)  # v4：收窄件数（原 1-30）剪金额长尾，提升信号信噪比
                amount = round(qty * PRODUCTS[product] * rng.uniform(0.9, 1.05), 2)
                sales_rows.append((cust["id"], day, region, product, qty, amount))
        day += timedelta(days=1)

    password = getpass.getpass("MySQL root 密码: ")
    conn = pymysql.connect(host=args.host, port=args.port, user="root",
                           password=password, database=args.db, charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE customers")
            cur.execute("TRUNCATE TABLE sales")
            cur.executemany(
                "INSERT INTO customers (id, name, phone, region, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                [(c["id"], c["name"], c["phone"], c["region"], c["created_at"])
                 for c in customers],
            )
            cur.executemany(
                "INSERT INTO sales (customer_id, order_date, region, product, quantity, amount) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                sales_rows,
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM customers")
            cc = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*), COUNT(DISTINCT customer_id) FROM sales")
            sc, dc = cur.fetchone()
        print(f"seeded customers={cc} sales={sc} (distinct customers in sales={dc})")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
