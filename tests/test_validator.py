from examples.schema import build_registry
from nl2sql.validation import SQLValidator


def test_valid_select_ok():
    v = SQLValidator(build_registry())
    err = v.validate(
        "SELECT SUM(total_amount) FROM orders WHERE status='paid'", ["orders"]
    )
    assert err is None


def test_forbid_write():
    v = SQLValidator(build_registry())
    err = v.validate("DELETE FROM orders", ["orders"])
    assert err is not None
    assert "写操作" in err


def test_table_whitelist():
    v = SQLValidator(build_registry())
    err = v.validate("SELECT * FROM secrets", ["orders"])
    assert err is not None
    assert "未授权表" in err


def test_column_hallucination():
    v = SQLValidator(build_registry())
    err = v.validate("SELECT amount FROM orders", ["orders"])
    assert err is not None
    assert "列不存在" in err


def test_parse_error():
    v = SQLValidator(build_registry())
    err = v.validate("SELECT FROM WHERE ???", ["orders"])
    assert err is not None
    assert "解析失败" in err
