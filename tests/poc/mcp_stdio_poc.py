"""Day 1 POC：stdio 模式跑通「查询表记录数」完整链路。

用法（conda activate data-analysis-agent 后）：
    python tests/poc/mcp_stdio_poc.py             # 默认查 analytics.sales
    python tests/poc/mcp_stdio_poc.py my_table    # 指定表名

依赖：自研 MCP Server（app/mcp/server.py，mcp SDK + pymysql）+ mcp（Python SDK）。
连接参数：优先读取项目根目录 .env（MYSQL_*），也可用环境变量覆盖。
本脚本不硬编码工具名假设之外的任何东西：先 tools/list 拉真实工具清单，
再按名字调用 execute_sql —— 工具名或 schema 变化会立刻暴露（Day 4 做工具列表
缓存 TTL=5min 时，本脚本的 tools/list 输出就是缓存数据源）。

输出：工具清单 JSON + 行数查询结果 + 各阶段耗时（毫秒），退出码 0/1。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time


def ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


async def main() -> int:
    table = sys.argv[1] if len(sys.argv) > 1 else "sales"

    try:  # 服务端自身也加载 .env，客户端再加载一次保证参数一致
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    command = os.environ.get("MYSQL_MCP_COMMAND", "python")
    default_args = ["-m", "app.mcp.server"]
    args = (json.loads(os.environ["MYSQL_MCP_ARGS"])
            if os.environ.get("MYSQL_MCP_ARGS") else default_args)
    params = StdioServerParameters(command=command, args=args, env=dict(os.environ))

    print(f"[poc] command={command} args={args} table={table}")

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            t0 = time.perf_counter()
            await session.initialize()
            print(f"[poc] initialize ok ({ms(t0):.1f} ms)")

            # ---- 工具清单（Day 4 缓存源：tools/list 结果 TTL 5min） ----
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("[poc] tools/list -> " + json.dumps(names, ensure_ascii=False))
            schema_bytes = sum(len(json.dumps(t.inputSchema)) for t in tools.tools)
            print(f"[poc] tool schema 总大小约 {schema_bytes} B（供缓存设计参考）")
            # 工具注解（readOnlyHint 等）——验证权限层能否识别只读声明
            annos = {
                t.name: t.annotations.model_dump(exclude_none=True) if t.annotations else None
                for t in tools.tools
            }
            print("[poc] tools annotations -> " + json.dumps(annos, ensure_ascii=False, default=str))

            if "execute_sql" not in names:
                print(f"[poc] FAIL: 未找到 execute_sql，实际工具集: {names}")
                return 1

            # ---- 行数查询 ----
            query = f"SELECT COUNT(*) AS row_count FROM `{table}`"
            t0 = time.perf_counter()
            res = await session.call_tool("execute_sql", {"query": query})
            latency = ms(t0)

            texts = [getattr(item, "text", str(item)) for item in res.content]
            payload = "\n".join(texts)
            print(f"[poc] execute_sql latency={latency:.1f} ms is_error={getattr(res, 'isError', False)}")
            print("[poc] result: " + payload)

            if getattr(res, "isError", False) or "row_count" not in payload:
                print("[poc] FAIL: 查询未返回预期结果")
                return 1

            print("[poc] PASS: 行数查询链路跑通（initialize + tools/list + execute_sql）")
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
