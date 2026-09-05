"""自研 MySQL MCP Server（Day 1 路线 A —— 用户已确认）。

为什么自研而不是用 mysql-mcp-server（designcomputer）：
  1. 该包 PyPI 上全部版本 Requires-Python >=3.11，与项目锁定的 Python 3.10 冲突（元数据核实）。
  2. 生产控制点应内建于自持代码，而非依赖第三方实现细节：
     连接池 / 查询超时 / 行数上限 / 只读强制 / EXPLAIN —— 正是 Day 4 量化指标的落点。

工具集（对齐原方案 4 个工具）：
  - execute_sql       只读执行 SQL（SELECT/SHOW/DESCRIBE/EXPLAIN），单语句强制，行数上限
  - get_schema_info   表结构（INFORMATION_SCHEMA 参数化查询）
  - get_table_sample  采样数据（limit <= 20）
  - explain_query     执行计划分析（原方案的 explain_query 由本工具实现）

安全三层防线：
  1. 应用层只读白名单 + 单语句强制（本模块，防注入）
  2. 表名白名单正则（堵住 SQL 拼接注入点）
  3. DB 层只读账户 mcp_reader（deploy/docker/mysql-init 已建，仅 SELECT）—— 最终兜底

性能控制：连接池（默认 20，范围 20-50）/ read_timeout=30s 查询超时 / MAX_ROWS=1000 行数上限。

运行：
  python -m app.mcp.server                                  # stdio（开发，Reasonix/Claude Code 子进程）
  MCP_TRANSPORT=streamable-http python -m app.mcp.server    # 独立 HTTP 服务（Day 4 生产，水平扩展）
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import functools
import time
import uuid

import pymysql
from dotenv import load_dotenv

load_dotenv()  # 服务端自动加载 .env（与 vendor 行为一致）

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402
from app.core.logging import logger  # noqa: E402

# ---------------- 配置（全部来自 .env / 环境变量，禁止硬编码） ----------------
HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
PORT = int(os.getenv("MYSQL_PORT", "3306"))
USER = os.getenv("MYSQL_USER", "mcp_reader")
PASSWORD = os.getenv("MYSQL_PASSWORD", "")
DATABASE = os.getenv("MYSQL_DATABASE", "")  # 留空 = 多库模式（INFORMATION_SCHEMA 可见全部用户库）
POOL_SIZE = max(1, min(int(os.getenv("MYSQL_POOL_SIZE", "20")), 50))  # 规范要求 20-50
QUERY_TIMEOUT = int(os.getenv("QUERY_TIMEOUT_SECONDS", "30"))
MAX_ROWS = int(os.getenv("MAX_ROWS_RETURN", "1000"))
PII_MASKING = os.getenv("PII_MASKING", "true").lower() in ("1", "true", "yes")  # 默认开
TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")
HTTP_HOST = os.getenv("MCP_SSE_HOST", "127.0.0.1")  # Day 4 独立服务监听地址
HTTP_PORT = int(os.getenv("MCP_SSE_PORT", "8000"))

# ---------------- 校验规则 ----------------
_READ_ONLY_RE = re.compile(r"^\s*(SELECT|SHOW|DESCRIBE|DESC|EXPLAIN)\b", re.I)
_TABLE_NAME_RE = re.compile(r"^[A-Za-z0-9_$]+(\.[A-Za-z0-9_$]+)?$")
_SQL_COMMENT_RE = re.compile(r"/\*.*?\*/|(--|#)[^\n]*")


def _strip_comments(sql: str) -> str:
    """去掉 SQL 注释（/* */、--、#），防注释绕过只读检查。"""
    return _SQL_COMMENT_RE.sub(" ", sql)


def _guard_read_only(sql: str) -> str:
    """只读 + 单语句强制。返回去掉末尾分号、可安全执行的 SQL。"""
    body = _strip_comments(sql).strip()
    if body.endswith(";"):
        body = body[:-1].rstrip()
    if ";" in body:
        raise ValueError("multi-statement SQL is not allowed")
    if not _READ_ONLY_RE.match(body):
        raise ValueError("only SELECT/SHOW/DESCRIBE/EXPLAIN allowed (read-only server)")
    return body


def _guard_table_name(name: str) -> str:
    """表名白名单：字母数字/下划线/$，允许一个 . 做 db.table 分隔。"""
    if not _TABLE_NAME_RE.match(name):
        raise ValueError("invalid table name (alnum/underscore/$ only, single dot separator)")
    return name


# ---------------- PII 脱敏（数据离开 server 前；列名驱动 + 值级手机号兜底） ----------------
_PII_COL_NAME = re.compile(r"(^|_)(name|realname|username|contact)(_|$)", re.I)
_PII_COL_PHONE = re.compile(r"(^|_)(phone|mobile|tel|telephone|cellphone)(_|$)", re.I)
_PII_COL_EMAIL = re.compile(r"(^|_)(email|mail)(_|$)", re.I)
_PII_PHONE_VALUE = re.compile(r"1[3-9]\d{9}")  # 值级兑底：任意文本列里裸奔的手机号


def _mask_phone(s: str) -> str:
    digits = re.sub(r"\D", "", s)
    if len(digits) < 7:
        return "*" * len(s)
    return s[:3] + "*" * max(1, len(s) - 7) + s[-4:]  # 138****0001


def _mask_value(key: str, value: object) -> object:
    if value is None:
        return None
    if _PII_COL_NAME.search(key):
        s = str(value)
        return (s[0] + "*" * max(1, len(s) - 1)) if s else s  # 张*
    if _PII_COL_PHONE.search(key):
        return _mask_phone(str(value))
    if _PII_COL_EMAIL.search(key):
        s = str(value)
        if "@" in s:
            local, _, domain = s.partition("@")
            return (local[:1] + "***@" + domain) if local else "***@" + domain
        return s
    if isinstance(value, str) and _PII_PHONE_VALUE.search(value):
        return _PII_PHONE_VALUE.sub(lambda m: _mask_phone(m.group()), value)
    return value


def _mask_row(row: dict) -> dict:
    """整行脱敏；PII_MASKING=false 时透传（逃生门，生产保持默认开）。"""
    if not PII_MASKING:
        return row
    return {k: _mask_value(k, v) for k, v in row.items()}


# ---------------- 连接池（队列实现，线程安全） ----------------
class _Pool:
    def __init__(self, size: int):
        self._queue: "queue.Queue[pymysql.Connection]" = queue.Queue(maxsize=size)
        for _ in range(size):
            self._queue.put(self._connect())

    def _connect(self) -> "pymysql.Connection":
        return pymysql.connect(
            host=HOST, port=PORT, user=USER, password=PASSWORD,
            database=DATABASE or None, charset="utf8mb4",
            connect_timeout=10,
            read_timeout=QUERY_TIMEOUT,  # 查询超时：服务端 30s 无响应即断开，慢查询不拖垮服务
            autocommit=True,
        )

    def acquire(self) -> "pymysql.Connection":
        # 池满时排队等待（与 agent-middleware 的 Semaphore 限流同理），超时即报错
        try:
            return self._queue.get(timeout=QUERY_TIMEOUT)
        except queue.Empty:
            # queue.Empty 消息为空 → MCP 客户端看到"空错误"（reasonix 实测）；补显式信息
            raise RuntimeError(
                f"connection pool exhausted after {QUERY_TIMEOUT}s (pool size={POOL_SIZE}); "
                "consider raising MYSQL_POOL_SIZE or lowering concurrency"
            ) from None

    def release(self, conn: "pymysql.Connection", healthy: bool = True) -> None:
        if not healthy:
            # 连接已坏：关闭并重建，避免毒连接回流
            try:
                conn.close()
            except Exception:
                pass
            conn = self._connect()
        self._queue.put(conn)


_pool: _Pool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> _Pool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _Pool(POOL_SIZE)
    return _pool


# ---------------- 执行 ----------------
def _execute(sql: str) -> tuple[list[dict], bool]:
    """执行只读 SQL 并返回行（应用行数上限），返回 (rows, truncated)。"""
    conn = _get_pool().acquire()
    healthy = True
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchmany(MAX_ROWS + 1)
            truncated = len(rows) > MAX_ROWS
            return rows[:MAX_ROWS] if truncated else rows, truncated
    except Exception:
        healthy = False
        raise
    finally:
        # 修复（Day 4 实测）：旧实现 release 放 else 子句——try 块一旦 return 就跳过 else，
        # 每次成功查询泄漏一个池连接（pool 20 时第 21 个请求起全部 acquire 超时）。
        # finally 在 return 与异常两条路径都执行，保证连接归还。
        _get_pool().release(conn, healthy=healthy)


# host/port 走构造函数（FastMCP.run() 不接受 host/port —— 1.x 实测）
mcp = FastMCP("mysql", host=HTTP_HOST, port=HTTP_PORT)

# 如实声明只读：本 server 全部工具都是 SELECT 只读（有守卫兜底）。
# Reasonix 权限层对 readOnlyHint: true 的工具默认放行只读会话 + 允许并行调度。
#
# 协议坑（Day 2 实测两连击）：
#   1. mcp 1.29.1 ToolAnnotations 无驼峰 alias → 线上序列化为 read_only_hint（snake_case），
#      违反 MCP 规范 readOnlyHint，Reasonix 按规范键解析认不出 → 只读会话拦截；
#   2. raw wire 探针实测：SDK 序列化不走 by_alias → alias_generator 无效。
# 解法：子类直接用规范驼峰名作 pydantic 字段名（字段名 = wire 键），绕开 alias 机制。
class _WireToolAnnotations(ToolAnnotations):
    readOnlyHint: bool | None = None


READ_ONLY_ANNOTATIONS = _WireToolAnnotations(readOnlyHint=True)


# ---------------- 可观测性：每工具调用一条 JSON 日志（stderr，含 trace_id/耗时） ----------------
def _logged(fn):
    """工具调用日志装饰器：trace_id + latency + 错误。日志走 stderr（stdout 留给协议）。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        trace_id = uuid.uuid4().hex[:12]
        t0 = time.perf_counter()
        try:
            out = fn(*args, **kwargs)
            logger.info("tool_ok", extra={
                "tool": fn.__name__,
                "trace_id": trace_id,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            })
            return out
        except Exception as exc:  # 记日志后原样抛出（FastMCP 转协议错误）
            logger.error("tool_error", extra={
                "tool": fn.__name__,
                "trace_id": trace_id,
                "error": str(exc)[:300],
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            })
            raise
    return wrapper


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
@_logged
def execute_sql(query: str) -> str:
    """只读执行 SQL 查询（仅 SELECT/SHOW/DESCRIBE/EXPLAIN），返回 JSON：{rows, row_count, truncated}。

    Args:
        query: 单条只读 SQL 语句。禁止多语句、禁止任何写操作。
    """
    sql = _guard_read_only(query)
    rows, truncated = _execute(sql)
    rows = [_mask_row(r) for r in rows]  # PII 脱敏：结果离开 server 前
    return json.dumps(
        {"rows": rows, "row_count": len(rows), "truncated": truncated},
        ensure_ascii=False, default=str,
    )


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
@_logged
def get_schema_info(table_name: str) -> str:
    """返回表结构：列名/类型/可空/默认值/注释。参数化查询（INFORMATION_SCHEMA）。

    Args:
        table_name: 表名；支持 database.table 跨库写法（如 analytics.sales）。
    """
    _guard_table_name(table_name)
    parts = table_name.split(".")
    schema, tbl = (parts[0], parts[1]) if len(parts) == 2 else (DATABASE or None, table_name)
    conn = _get_pool().acquire()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = COALESCE(%s, DATABASE()) AND TABLE_NAME = %s "
                "ORDER BY ORDINAL_POSITION",
                (schema, tbl),
            )
            rows = cur.fetchall()
    finally:
        _get_pool().release(conn)
    return json.dumps({"table": table_name, "columns": rows}, ensure_ascii=False, default=str)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
@_logged
def get_table_sample(table_name: str, limit: int = 20) -> str:
    """采样表数据（最多 20 行），快速了解字段格式与取值分布。

    Args:
        table_name: 表名，支持 database.table 跨库写法。
        limit: 采样行数，范围 1-20。
    """
    _guard_table_name(table_name)
    limit = max(1, min(int(limit), 20))
    conn = _get_pool().acquire()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            # 表名已过白名单正则（无引号/反引号/分号），可安全拼接；limit 走参数化
            cur.execute(f"SELECT * FROM {table_name} LIMIT %s", (limit,))
            rows = cur.fetchall()
    finally:
        _get_pool().release(conn)
    rows = [_mask_row(r) for r in rows]  # PII 脱敏：采样结果同样过掩码
    return json.dumps(rows, ensure_ascii=False, default=str)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
@_logged
def explain_query(query: str) -> str:
    """对 SQL 生成执行计划（EXPLAIN），用于慢查询分析，不执行数据查询。

    Args:
        query: 待分析的只读 SQL 语句。
    """
    sql = _guard_read_only(query)
    if not re.match(r"^\s*EXPLAIN\b", sql, re.I):
        sql = "EXPLAIN " + sql
    rows, truncated = _execute(sql)
    return json.dumps({"rows": rows, "truncated": truncated}, ensure_ascii=False, default=str)


def main() -> None:
    transport = TRANSPORT if TRANSPORT in ("stdio", "streamable-http") else "stdio"
    if transport == "streamable-http":
        _get_pool()  # 启动即建池：连接失败在启动期快速暴露（systemd/k8s 就绪探针友好）
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
