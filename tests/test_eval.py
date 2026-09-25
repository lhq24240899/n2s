"""评估 harness 的离线单测：比对逻辑不碰网络、不碰库。"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.harness import evaluate, is_readonly_sql, norm_rows, norm_value


def _payload(rows, cols=("v",), ptype="result", **kw):
    p = {"type": ptype, "rows": rows, "columns": list(cols)}
    p.update(kw)
    return p


# ---------------- 归一化 ----------------

def test_norm_value_rounds_floats():
    assert norm_value(0.9166666666) == 0.9167
    assert norm_value(Decimal("3200000.00")) == 3200000.0
    assert norm_value(12) == 12.0
    assert norm_value(None) is None
    assert norm_value("  华东 ") == "华东"


def test_norm_rows_is_order_insensitive():
    a = norm_rows(["r", "v"], [("华东", 0.9), ("华南", 0.8889)])
    b = norm_rows(["r", "v"], [("华南", 0.88885), ("华东", 0.9)])
    assert a == b


# ---------------- 只读判定 ----------------

def test_readonly_sql():
    assert is_readonly_sql("SELECT 1")
    assert is_readonly_sql("  with t as (select 1) select * from t")
    assert not is_readonly_sql("DELETE FROM reports")
    assert not is_readonly_sql(None)
    assert not is_readonly_sql("")


# ---------------- kind 型期望 ----------------

def test_no_write_accepts_denied_and_readonly_result():
    case = {"expect": {"kind": "no_write"}}
    ok, _ = evaluate(case, {"type": "denied"})
    assert ok
    ok, _ = evaluate(case, _payload([[5]], sql="SELECT COUNT(*) FROM reports"))
    assert ok
    ok, why = evaluate(case, _payload([[5]], sql="DELETE FROM reports"))
    assert not ok and "写语义" in why


def test_rag_requires_citations():
    case = {"expect": {"kind": "rag"}}
    ok, _ = evaluate(case, {"type": "rag", "citations": [{"title": "x"}]})
    assert ok
    ok, _ = evaluate(case, {"type": "rag", "citations": []})
    assert not ok
    ok, _ = evaluate(case, _payload([[1]]))
    assert not ok


def test_clarification_and_any():
    ok, _ = evaluate({"expect": {"kind": "clarification"}}, {"type": "clarification"})
    assert ok
    ok, _ = evaluate({"expect": {"kind": "any"}}, {"type": "denied"})
    assert ok
    ok, _ = evaluate({"expect": {"kind": "any"}}, {"type": "weird"})
    assert not ok


# ---------------- 数值 / 行集比对 ----------------

def test_value_match_with_float_tolerance():
    case = {"compare": "value"}
    ok, why = evaluate(case, _payload([[0.91666666]]), None, [[Decimal("0.9166666666")]])
    assert ok and "0.9167" in why


def test_value_allows_extra_context_columns():
    """「最高的实验室」允许系统顺带返回利用率列。"""
    case = {"compare": "value"}
    ok, _ = evaluate(case, _payload([["上海集成电路实验室", 0.88]]), None, [["上海集成电路实验室"]])
    assert ok
    ok, _ = evaluate(case, _payload([["北京电磁兼容实验室", 0.79]]), None, [["上海集成电路实验室"]])
    assert not ok


def test_value_fails_on_wrong_shape():
    case = {"compare": "value"}
    ok, _ = evaluate(case, _payload([]), None, [[1]])
    assert not ok
    ok, _ = evaluate(case, {"type": "clarification"}, None, [[1]])
    assert not ok


def test_rows_multiset_compare():
    case = {"compare": "rows"}
    truth_cols, truth = ["name", "v"], [("a", 0.9), ("b", 0.8)]
    ok, _ = evaluate(case, _payload([[ "b", 0.8], ["a", 0.9]], cols=("name", "v")), truth_cols, truth)
    assert ok
    ok, why = evaluate(case, _payload([[ "a", 0.9]], cols=("name", "v")), truth_cols, truth)
    assert not ok and "行集不一致" in why


def test_value_fails_when_type_is_not_result():
    case = {"compare": "value"}
    ok, why = evaluate(case, {"type": "rag", "citations": []}, None, [[1]])
    assert not ok and "期望 result" in why
