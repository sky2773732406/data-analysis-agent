"""Day 5 高可用演练（本地自动化）：杀服务器 → 重启 → 客户端恢复。

原理：MCP Server 无状态（4.1 验证）→ 实例故障只影响在途请求；
新实例拉起后新连接应完整可用 = 恢复成功（无会话状态可丢失）。

本地单机模拟（systemd/K8s 的自愈由部署层负责，原理相同）：
  1. 在独立端口 8001 拉起 server 子进程
  2. 跑一批调用（pre-kill 基线，应全成功）
  3. 杀掉 server（模拟实例故障；在途请求应报错 = 预期，不计入失败判据）
  4. 重新拉起新实例（模拟自动恢复）
  5. 再跑一批调用（post-restart，应全成功 = 恢复验证通过）

注意：先停掉终端 1 占用的 8000 端口服务不影响本脚本（脚本用 8001）。
用法：python tests/poc/ha_drill.py
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time

PORT = 8001
URL = f"http://127.0.0.1:{PORT}/mcp"
QUERY = "SELECT COUNT(*) AS c FROM sales"


async def run_batch(tag: str, n: int = 30) -> tuple[int, int]:
    """并发跑 n 次调用，返回 (成功数, 失败数)。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def once() -> bool:
        try:
            async with streamable_http_client(URL) as (read, write, _sid):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    res = await session.call_tool("execute_sql", {"query": QUERY})
                    return not getattr(res, "isError", False)
        except Exception:
            return False

    results = await asyncio.gather(*[once() for _ in range(n)])
    ok = sum(1 for r in results if r)
    print(f"[ha] {tag}: ok={ok}/{n}")
    return ok, n - ok


def spawn_server() -> subprocess.Popen:
    env = {**os.environ,
           "MCP_TRANSPORT": "streamable-http",
           "MCP_SSE_PORT": str(PORT),
           "MYSQL_POOL_SIZE": "5"}  # 演练用小池
    return subprocess.Popen([sys.executable, "-m", "app.mcp.server"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_ready(proc: subprocess.Popen, timeout: float = 20.0) -> bool:
    """轮询 TCP 端口直到可连（就绪探针的本地模拟）。"""
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False  # 进程已退出 = 启动失败
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def main() -> int:
    print(f"[ha] 端口 {PORT} 演练开始（先确保 8001 未被占用）")
    server = spawn_server()
    if not wait_ready(server):
        print("[ha] FAIL: server 首次启动未就绪")
        return 1
    print("[ha] server-1 就绪")

    ok1, _ = asyncio.run(run_batch("pre-kill 基线", 30))
    if ok1 != 30:
        print("[ha] FAIL: 基线调用未全成功")
        return 1

    print("[ha] 杀掉 server-1（模拟实例故障）...")
    server.kill()
    server.wait()
    # 在途/新请求预期失败（此刻无实例）——仅观察，不计判据
    _, _ = asyncio.run(run_batch("故障窗口(预期失败)", 10))

    print("[ha] 拉起 server-2（模拟自动恢复）...")
    server2 = spawn_server()
    if not wait_ready(server2):
        print("[ha] FAIL: server-2 未就绪")
        return 1
    print("[ha] server-2 就绪")

    ok2, fail2 = asyncio.run(run_batch("post-restart 恢复验证", 30))
    server2.kill()
    print(f"[ha] {'PASS' if ok2 == 30 else 'FAIL'}: 恢复后 ok={ok2} fail={fail2} "
          f"（无状态：新实例无会话状态可丢，连接即用）")
    return 0 if ok2 == 30 else 1


if __name__ == "__main__":
    sys.exit(main())
