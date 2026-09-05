"""PII 脱敏纯函数单元测试（无需数据库）。

覆盖：列名驱动的 name/phone/email 掩码、值级手机号兜底、非 PII 列透传、
开关 PII_MASKING=false 时透传。
"""
import pytest

from app.mcp.server import _mask_row, _mask_value


@pytest.mark.parametrize("key,value,expected", [
    ("name", "张伟", "张*"),
    ("customer_name", "李娜敏", "李**"),
    ("username", "admin", "a****"),
    ("name", "", ""),
    ("phone", "13800000001", "138****0001"),
    ("mobile", "13912345678", "139****5678"),
    ("phone", "123", "***"),
    ("email", "zhangwei@example.com", "z***@example.com"),
    ("remark", "联系 13800138000 王五", "联系 138****8000 王五"),  # 值级兜底
    ("amount", 12345.67, 12345.67),                            # 非 PII 透传
    ("region", "华东", "华东"),
    ("name", None, None),
])
def test_mask_value(key, value, expected):
    assert _mask_value(key, value) == expected


def test_mask_row_keeps_other_columns():
    row = {"id": 1, "name": "张伟", "phone": "13800000001", "region": "华东", "amount": 999.0}
    out = _mask_row(row)
    assert out == {"id": 1, "name": "张*", "phone": "138****0001", "region": "华东", "amount": 999.0}
    # 原行不被修改（不可变处理）
    assert row["name"] == "张伟"
