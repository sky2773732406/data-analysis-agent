"""只读守卫纯函数单元测试（无需数据库/网络，pytest 直接跑）。

运行：conda activate data-analysis-agent && python -m pytest tests/unit -q
"""
import pytest

from app.mcp.server import _guard_read_only, _guard_table_name


@pytest.mark.parametrize("sql,expected", [
    ("SELECT COUNT(*) FROM sales", "SELECT COUNT(*) FROM sales"),
    ("  select 1;", "select 1"),
    ("/* header */ SELECT 1", "SELECT 1"),
    ("-- comment\nSELECT 1", "SELECT 1"),
    ("# hash comment\nSHOW TABLES", "SHOW TABLES"),
])
def test_guard_read_only_accepts(sql, expected):
    assert _guard_read_only(sql) == expected


@pytest.mark.parametrize("sql", [
    "DELETE FROM sales",
    "INSERT INTO sales VALUES (1, 2)",
    "UPDATE sales SET amount = 0",
    "DROP TABLE sales",
    "TRUNCATE TABLE sales",
    "SELECT 1; DROP TABLE sales",   # 多语句注入
    "SELECT 1; SELECT 2",           # 分号分隔多语句
    "SET GLOBAL sql_mode = ''",     # 非白名单语句
])
def test_guard_read_only_rejects(sql):
    with pytest.raises(ValueError):
        _guard_read_only(sql)


@pytest.mark.parametrize("name", ["sales", "analytics.sales", "order_$1", "tbl2026"])
def test_guard_table_name_accepts(name):
    assert _guard_table_name(name) == name


@pytest.mark.parametrize("name", [
    "sales; DROP TABLE x",
    "`sales`",
    "sales--",
    "a.b.c",          # 两个点：不支持
    "sales/../x",
    "'' OR '1'='1",
])
def test_guard_table_name_rejects(name):
    with pytest.raises(ValueError):
        _guard_table_name(name)
