"""Raw wire 探针：打印 tools/list 的原始 JSON-RPC 响应（不经 SDK 模型解析）。

为什么需要：mcp Python SDK 客户端模型会把线上 JSON 重新映射成模型字段，
用 POC 客户端看到的键名不等于线上真实键名。要验证 readOnlyHint 注解在 wire 上
到底是 camelCase（协议规范）还是 snake_case（mcp 1.29.1 序列化偏差），
必须直接看服务器进程的 stdout 原始字节。

用法：python tests/poc/raw_tools_list.py
预期修复后输出：wire 上出现 "annotations":{"readOnlyHint":true}
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


def main() -> int:
    payload = "\n".join([
        json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "wire-probe", "version": "0"},
            },
        }),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        "",
    ]).encode()

    proc = subprocess.Popen(
        [sys.executable, "-m", "app.mcp.server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        out, err = proc.communicate(payload, timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        print("FAIL: server 15s 未响应")
        return 1

    text = out.decode(errors="replace")
    if err:
        print("server stderr:", err.decode(errors="replace")[:500])
    idx = text.find("annotations")
    if idx >= 0:
        print("wire annotations 片段:", text[max(0, idx - 40):idx + 100])
        return 0 if '"readOnlyHint"' in text else 1
    print("FAIL: wire 上未见 annotations 键（整段响应前 300 字符：）")
    print(text[:300])
    return 1


if __name__ == "__main__":
    sys.exit(main())
