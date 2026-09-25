"""多轮上下文单元测试：inherit 把上一轮维度回填到本轮缺失项。"""
from __future__ import annotations

from nl2sql.context import QueryContext
from nl2sql.semantic import MappedQuery, Metric


def _mapped(question, metric=None, entities=None) -> MappedQuery:
    return MappedQuery(
        original=question,
        normalized=question,
        metric=metric,
        entities=entities or {},
        resolved_synonyms=[],
        clarification=None,
        reasons=[],
    )


def test_inherit_fills_region():
    ctx = QueryContext(
        metric=Metric("m", "检测准时率", "运营级", "通用", "x", "y"),
        business_line="reliability",
        region="华东",
        time="上个月",
    )
    # 追问只说"华南区"，其余应继承自上一轮
    merged = ctx.inherit(_mapped("那华南区呢", entities={"region": "华南"}))
    assert merged.entities["region"] == "华南"
    assert merged.entities["business_line"] == "reliability"
    assert merged.entities["time"] == "上个月"
    assert merged.metric is not None
    # 归一化问题应带上继承的业务线 code，保证检索/生成可见
    assert "reliability" in merged.normalized


def test_explicit_overrides_context():
    ctx = QueryContext(region="华东")
    merged = ctx.inherit(_mapped("华北区收入", entities={"region": "华北"}))
    assert merged.entities["region"] == "华北"  # 显式说华北，覆盖华东


def test_inherit_skips_grouped_dimension():
    """本轮要「按业务线分组」时，不能把上一轮的业务线过滤继承进来。

    回归用例：先问「华东区可靠性…」，再问「各业务线的准时率」，
    若沿用 business_line=reliability，结果只剩一个数值。
    """
    ctx = QueryContext(business_line="reliability", region="华东")
    merged = ctx.inherit(_mapped("各业务线的检测准时率", entities={"group_by": "business_line"}))

    assert "business_line" not in merged.entities, "分组维度不应被继承成过滤条件"
    assert merged.entities["group_by"] == "business_line"
    assert merged.entities["region"] == "华东", "其他维度仍按多轮规则继承"
    assert any("分组优先" in r for r in merged.reasons)


def test_inherit_still_fills_when_not_grouped():
    ctx = QueryContext(business_line="reliability")
    merged = ctx.inherit(_mapped("准时率是多少"))
    assert merged.entities["business_line"] == "reliability"
