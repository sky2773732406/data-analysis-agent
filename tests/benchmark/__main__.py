"""压测模块（Day 4 单实例基准 → Day 5 扩展线性度复用）。

用法（先确保 HTTP MCP Server 在跑）：
    conda activate data-analysis-agent
    $env:MCP_TRANSPORT = "streamable-http"
    python -m app.mcp.server                        # 终端 1
    python -m tests.benchmark --concurrency 50      # 终端 2（Day 4 指标：50 并发 P95 < 3s）

参数：
    --url           默认 http://127.0.0.1:8000/mcp
    --concurrency   并发客户端数（默认 50）
    --per-client    每客户端请求数（默认 20 → 总 ~1000 次调用）
    --mix           查询混合：count(70%) + groupby(30%)，贴近真实 Agent 负载

退出码：0 = P95 < 3000ms 且错误率 < 1%（Day 4 指标门）；否则 1。
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time


def _percentile(sorted_lat: list[float], p: float) -> float:
    if not sorted_lat:
        return 0.0
    idx = min(len(sorted_lat) - 1, int(p / 100 * len(sorted_lat)))
    return sorted_lat[idx]


async def _worker(url: str, queries: list[str], per_client: int,
                  results: list, errors: list, idx: int) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    try:
        async with streamable_http_client(url) as (read, write, _sid):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for i in range(per_client):
                    query = queries[(idx + i) % len(queries)]
                    t0 = time.perf_counter()
                    try:
                        await session.call_tool("execute_sql", {"query": query})
                        results.append((time.perf_counter() - t0) * 1000)
                    except Exception:
                        errors.append(1)
    except Exception:
        errors.append(1)  # 连接级失败也计入错误率


async def run(args) -> dict:
    queries = [
        "SELECT COUNT(*) AS c FROM sales",                                # 轻量 count
        "SELECT DATE_FORMAT(order_date, '%Y-%m') AS ym, SUM(amount) AS a "
        "FROM sales GROUP BY ym ORDER BY ym",                             # 聚合 groupby
    ]
    if args.mix:  # count × 7 : groupby × 3
        queries = queries[:1] * 7 + queries[1:] * 3

    results: list[float] = []
    errors: list[int] = []
    t0 = time.perf_counter()
    await asyncio.gather(*[
        _worker(args.url, queries, args.per_client, results, errors, i)
        for i in range(args.concurrency)
    ])
    duration = time.perf_counter() - t0

    total = len(results) + len(errors)
    error_rate = len(errors) / total if total else 0.0
    sorted_lat = sorted(results)
    qps = total / duration if duration > 0 else 0.0
    return {
        "concurrency": args.concurrency,
        "total_calls": total,
        "ok": len(results), "errors": len(errors),
        "error_rate": round(error_rate, 4),
        "duration_s": round(duration, 2),
        "qps": round(qps, 1),
        "p50_ms": round(_percentile(sorted_lat, 50), 1),
        "p95_ms": round(_percentile(sorted_lat, 95), 1),
        "p99_ms": round(_percentile(sorted_lat, 99), 1),
        "max_ms": round(sorted_lat[-1], 1) if sorted_lat else 0.0,
        "avg_ms": round(statistics.mean(sorted_lat), 1) if sorted_lat else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    ap.add_argument("--concurrency", type=int, default=50)
    ap.add_argument("--per-client", type=int, default=20)
    ap.add_argument("--mix", action="store_true", default=True)
    args = ap.parse_args()

    print(f"[bench] url={args.url} concurrency={args.concurrency} "
          f"per_client={args.per_client} total≈{args.concurrency * args.per_client}")
    stats = asyncio.run(run(args))
    print(json_like(stats))
    ok = stats["p95_ms"] < 3000 and stats["error_rate"] < 0.01
    print(f"[bench] {'PASS' if ok else 'FAIL'}: P95 {stats['p95_ms']}ms (<3000ms) "
          f"error_rate {stats['error_rate']:.2%} (<1%)")
    return 0 if ok else 1


def json_like(stats: dict) -> str:
    return "  " + "\n  ".join(f"{k}={v}" for k, v in stats.items())


if __name__ == "__main__":
    sys.exit(main())
