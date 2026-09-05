"""确定性核对：注入异常信号是否存在于数据、周同比能否命中。

背景：reasonix 里 LLM 检测有随机性与宿主护栏开销；数据级事实先用确定性脚本钉死
（与 Day 1 POC 同理：事实走确定性验证，LLM 只做最终端到端验收）。

回答三个问题：
  1. 注入窗口的逐日数据是否明显异于相邻日（信号存在性）
  2. 周同比（同区域上周比）在该窗口的偏离度
  3. 该偏离度在全部区域-周中的百分位（越高/越低越易被检出）

用法：python tests/poc/check_anomaly_signal.py
依赖：.env 中 MYSQL_*（mcp_reader 只读即可）
"""
from __future__ import annotations

import os
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

import pymysql

TARGETS = [
    # (区域, 窗口起, 窗口止, 注入说明)
    ("华南", date(2026, 3, 9), date(2026, 3, 15), "×3 尖峰注入（3/10-14）"),
    ("华北", date(2026, 4, 20), date(2026, 4, 26), "×0.2 低谷注入（4/20-24）"),
]


def connect() -> pymysql.Connection:
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "mcp_reader"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "analytics"),
        charset="utf8mb4",
        read_timeout=30,
    )


def main() -> int:
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT region, order_date, COUNT(*) AS n, SUM(amount) AS amt "
        "FROM sales GROUP BY region, order_date ORDER BY region, order_date"
    )
    daily: dict[tuple[str, date], tuple[int, float]] = {}
    for region, d, n, amt in cur.fetchall():
        daily[(region, d)] = (n, float(amt))
    conn.close()

    # 周汇总（ISO 周），周环比 = 本周/上周同区域 - 1
    weekly: dict[tuple[str, tuple[int, int]], float] = {}
    for (region, d), (_, amt) in daily.items():
        iso = d.isocalendar()
        key = (region, (iso[0], iso[1]))
        weekly[key] = weekly.get(key, 0.0) + amt

    pcts: dict[tuple[str, tuple[int, int]], float] = {}
    regions = sorted({k[0] for k in weekly})
    for region in regions:
        rw = sorted(k for k in weekly if k[0] == region)
        for i in range(1, len(rw)):
            k, pk = rw[i], rw[i - 1]
            if weekly[pk] > 0:
                pcts[k] = weekly[k] / weekly[pk] - 1.0

    all_pcts = sorted(pcts.values())

    def percentile(p: float) -> float:
        return 100.0 * sum(1 for x in all_pcts if x <= p) / len(all_pcts)

    def week_key(region: str, d: date) -> tuple[str, tuple[int, int]]:
        iso = d.isocalendar()
        return (region, (iso[0], iso[1]))

    print(f"总区域-周数: {len(weekly)}，有周环比的: {len(pcts)}")
    print("\n=== 目标窗口核对 ===")
    for region, s, e, note in TARGETS:
        wk = week_key(region, s)
        amt = weekly.get(wk, 0.0)
        p = pcts.get(wk)
        prev_wk = week_key(region, s - timedelta(days=7))
        prev_amt = weekly.get(prev_wk, 0.0)
        pct_str = f"{p:+.1%}" if p is not None else "无上周参照"
        rank_str = f"百分位 {percentile(p):.0f}%" if p is not None else "-"
        print(f"\n[{region}] {note}  窗口周 {s} ~ {e}")
        print(f"  窗口周营收 {amt:,.0f} vs 上周 {prev_amt:,.0f} → 周环比 {pct_str}（{rank_str}）")
        # 逐日明细（窗口前后各 2 天），信号存在性肉眼核对
        print("  逐日(前2天~窗口~后2天):")
        d = s - timedelta(days=2)
        while d <= e + timedelta(days=2):
            n, amt_d = daily.get((region, d), (0, 0.0))
            mark = "  <<<" if s <= d <= e else ""
            print(f"    {d}  单数 {n:>2}  金额 {amt_d:>12,.0f}{mark}")
            d += timedelta(days=1)

    print("\n=== 全部区域-周环比 top5 / bottom5（上下文） ===")
    ordered = sorted(pcts.items(), key=lambda kv: kv[1])
    for label, sl in (("bottom5", ordered[:5]), ("top5", ordered[-5:])):
        print(f"  {label}:")
        for (region, iso), p in sl:
            print(f"    {region} {iso[0]}-W{iso[1]:02d}: {p:+.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
