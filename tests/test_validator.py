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


def test_select_alias_is_allowed_in_order_by():
    """SELECT 别名不是物理列但完全合法——误杀会把 TopN 问句全部打回（评估集实测）。"""
    from examples.grg_schema import build_registry as grg_registry

    from nl2sql.validation import SQLValidator

    v = SQLValidator(grg_registry(), dialect="postgres")
    sql = (
        "SELECT b.name, COUNT(*) AS report_count FROM reports r "
        "JOIN business_lines b ON r.business_line_id = b.id "
        "GROUP BY b.name ORDER BY report_count DESC LIMIT 1"
    )
    assert v.validate(sql, ["reports", "business_lines"]) is None


def test_hallucinated_still_blocked():
    from examples.grg_schema import build_registry as grg_registry

    from nl2sql.validation import SQLValidator

    v = SQLValidator(grg_registry(), dialect="postgres")
    sql = (
        "SELECT b.name, COUNT(*) AS report_count FROM reports r "
        "JOIN business_lines b ON r.business_line_id = b.id "
        "GROUP BY b.name ORDER BY ghost_col DESC LIMIT 1"
    )
    assert v.validate(sql, ["reports", "business_lines"]) is not None
