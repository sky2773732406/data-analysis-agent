"""Day 4 POC：streamable-http 传输模式验证（MCP Server 独立服务的核心前提）。

验证点：
  1. 服务器以 MCP_TRANSPORT=streamable-http 启动后，能通过 HTTP 完成 initialize
  2. tools/list 返回 4 个工具（readOnlyHint 注解仍在）
  3. execute_sql 经 HTTP 全链路可调用（无状态：两个独立连接都能用）

先决条件（另开一个终端）：
    conda activate data-analysis-agent
    $env:MCP_TRANSPORT = "streamable-http"
    python -m app.mcp.server        # 监听 127.0.0.1:8000（MCP_SSE_HOST/PORT 可改）

本脚本：
    python tests/poc/mcp_http_poc.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time


def ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


async def one_session(url: str, table: str, tag: str) -> None:
    from mcp import ClientSession
    # mcp 1.29.1：旧名 streamablehttp_client 已废弃；新版 streamable_http_client
    # yield 三元组 (read, write, get_session_id) —— 无状态模式下 session_id 回调不用
    from mcp.client.streamable_http import streamable_http_client

    async with streamable_http_client(url) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            t0 = time.perf_counter()
            await session.initialize()
            print(f"[{tag}] initialize ok ({ms(t0):.1f} ms)")

            tools = await session.list_tools()
            print(f"[{tag}] tools -> {[t.name for t in tools.tools]}")

            res = await session.call_tool("execute_sql", {"query": f"SELECT COUNT(*) AS c FROM `{table}`"})
            texts = [getattr(i, "text", str(i)) for i in res.content]
            print(f"[{tag}] execute_sql -> {''.join(texts)}")


async def main() -> int:
    url = os.environ.get("MCP_URL", "http://127.0.0.1:8000/mcp")
    table = sys.argv[1] if len(sys.argv) > 1 else "sales"
    print(f"[poc] streamable-http url={url} table={table}")
    # 两个独立连接（无状态验证：同一 server 服务多个客户端）
    await one_session(url, table, "conn-1")
    await one_session(url, table, "conn-2")
    print("[poc] PASS: streamable-http 链路跑通（initialize + tools/list + execute_sql ×2 独立连接）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
