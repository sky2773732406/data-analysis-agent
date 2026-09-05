"""结构化 JSON 日志（写入 stderr）。

**关键约束**：MCP stdio 协议要求子进程 stdout 只承载 JSON-RPC 消息——
任何日志写 stdout 都会污染协议、导致客户端"Connection closed"。
因此本模块所有日志一律走 stderr（与 mcp 官方 server 日志行为一致）。

用法：
    from app.core.logging import logger
    logger.info("tool_ok", extra={"tool": "execute_sql", "latency_ms": 12.3})
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

# 允许随日志输出携带的 extra 字段（白名单，防任意注入）
_EXTRA_FIELDS = ("tool", "trace_id", "latency_ms", "row_count",
                 "truncated", "error", "query", "table_name", "region")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key in _EXTRA_FIELDS:
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(level: str | None = None) -> logging.Logger:
    """配置 daa logger（幂等）。level 取 env LOG_LEVEL，默认 INFO。"""
    logger = logging.getLogger("daa")
    if not logger.handlers:  # 幂等：已有 handler 则跳过
        logger.setLevel((level or os.getenv("LOG_LEVEL", "INFO")).upper())
        handler = logging.StreamHandler(sys.stderr)  # 协议安全：绝不写 stdout
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    return logger


logger = setup_logging()
