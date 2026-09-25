"""数据权限策略单测：表/列/指标白名单 + 行级过滤注入（sqlglot AST 改写）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nl2sql.auth import Principal, Role
from nl2sql.models import TableSchema
from nl2sql.policy import (
    DataPolicy,
    PolicyGuard,
    PolicyViolation,
    RowFilter,
    apply_row_filters,
)
from nl2sql.semantic import Metric


def _policy(**kw) -> DataPolicy:
    kw.setdefault("dialect", "postgres")
    return DataPolicy(**kw)


# ---------------- ① 生成前：摘掉无权表 ----------------

def test_constrain_schemas_hides_unauthorized_tables():
    schemas = [TableSchema(name="reports", columns={"id": "int"}),
               TableSchema(name="contracts", columns={"amount": "decimal"})]
    guard = PolicyGuard(_policy(allowed_tables=frozenset({"reports"})))
    kept = guard.constrain_schemas(schemas)
    assert [s.name for s in kept] == ["reports"]
    assert any("已隐藏无权表" in s for s in guard.applied)


def test_constrain_schemas_no_limit_when_none():
    schemas = [TableSchema(name="a", columns={"id": "int"})]
    assert PolicyGuard(_policy()).constrain_schemas(schemas) == schemas


# ---------------- ② 生成前：指标级拒绝 ----------------

def test_check_metric_denies_unlisted_metric():
    m = Metric(id="detect_service_revenue", name="检测服务收入", level="集团级",
               domain="通用", definition="口径", sql_hint="SUM(amount)")
    guard = PolicyGuard(_policy(allowed_metrics=frozenset({"on_time_completion_rate"})))
    with pytest.raises(PolicyViolation) as e:
        guard.check_metric(m)
    assert "无权查询指标" in str(e.value)

    ok = PolicyGuard(_policy(allowed_metrics=frozenset({"detect_service_revenue"})))
    ok.check_metric(m)  # 不抛异常
    PolicyGuard(_policy()).check_metric(m)  # allowed_metrics=None 表示不限制


# ---------------- ③ 生成后：列级拦截 ----------------

def test_denied_column_blocks_sql():
    guard = PolicyGuard(_policy(denied_columns=frozenset({"customers.phone", "phone"})))
    sql, err = guard.post_sql("SELECT c.name, c.phone FROM customers c")
    assert err and "未授权字段" in err
    assert sql  # 原 SQL 原样返回，由上层决定重生成


def test_bare_column_name_also_blocked():
    guard = PolicyGuard(_policy(denied_columns=frozenset({"phone"})))
    _, err = guard.post_sql("SELECT phone FROM customers")
    assert err and "未授权字段" in err


def test_normal_sql_passes_column_check():
    guard = PolicyGuard(_policy(denied_columns=frozenset({"phone"})))
    sql, err = guard.post_sql("SELECT c.name FROM customers c")
    assert err is None and sql == "SELECT c.name FROM customers c"


# ---------------- ③ 生成后：行级过滤注入 ----------------

def test_inject_row_filter_with_alias():
    sql = ("SELECT AVG(e.utilization) AS u FROM equipment e "
           "JOIN labs l ON e.lab_id = l.id")
    out, notes = apply_row_filters(sql, [RowFilter("labs", "region", ("华东",))])
    assert "l.region = '华东'" in out
    assert any("已注入行级过滤" in n for n in notes)


def test_inject_multiple_values_uses_in():
    out, _ = apply_row_filters(
        "SELECT COUNT(*) FROM reports r JOIN labs l ON r.lab_id = l.id",
        [RowFilter("labs", "region", ("华东", "华南"))],
    )
    assert "l.region IN ('华东', '华南')" in out


def test_inject_into_existing_where_keeps_original_condition():
    sql = ("SELECT COUNT(*) FROM reports r JOIN labs l ON r.lab_id = l.id "
           "WHERE r.on_time = 1")
    out, _ = apply_row_filters(sql, [RowFilter("labs", "region", ("华南",))])
    assert "r.on_time = 1" in out and "l.region = '华南'" in out
    # 两个条件必须同时生效（AND），不能把原条件覆盖掉
    assert " AND " in out.upper()


def test_inject_into_cte():
    sql = ("WITH t AS (SELECT l.region AS region, COUNT(*) AS n FROM reports r "
           "JOIN labs l ON r.lab_id = l.id GROUP BY 1) SELECT * FROM t")
    out, notes = apply_row_filters(sql, [RowFilter("labs", "region", ("华东",))])
    assert "l.region = '华东'" in out
    assert any("已注入" in n for n in notes)


def test_unreferenced_table_reports_not_applied():
    """SQL 没引用受限表时如实说"未生效"，绝不假装过滤成功。"""
    out, notes = apply_row_filters(
        "SELECT COUNT(*) FROM reports", [RowFilter("labs", "region", ("华东",))]
    )
    assert out == "SELECT COUNT(*) FROM reports"
    assert any("未生效" in n for n in notes)


def test_post_sql_combines_column_check_and_row_filter():
    guard = PolicyGuard(_policy(row_filters=(RowFilter("labs", "region", ("华东",)),)))
    sql, err = guard.post_sql(
        "SELECT COUNT(*) FROM reports r JOIN labs l ON r.lab_id = l.id"
    )
    assert err is None
    assert "l.region = '华东'" in sql
    assert guard.applied  # 授权事实被记录，可随响应返回（可解释）


# ---------------- Principal -> DataPolicy 合成 ----------------

def test_policy_from_principal_builds_row_filters():
    p = Principal.of("m", Role.ANALYST, regions=["华东"], business_lines=["reliability"])
    policy = DataPolicy.from_principal(p, dialect="postgres")
    fields = {(f.table, f.column) for f in policy.row_filters}
    assert ("labs", "region") in fields
    assert ("business_lines", "code") in fields
    assert policy.is_scoped
    assert any("行级过滤" in d for d in policy.describe())


def test_policy_from_principal_unscoped_when_no_limits():
    policy = DataPolicy.from_principal(Principal.of("m", Role.ANALYST))
    assert not policy.is_scoped
