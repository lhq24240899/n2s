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


# ---------------- 继承信息与「空结果回退」用的降级继承 ----------------

def test_inherited_dimensions_are_reported():
    ctx = QueryContext(metric=Metric("m", "检测准时率", "运营级", "通用", "x", "y"),
                       business_line="reliability", region="华东", time="上个月")
    merged = ctx.inherit(_mapped("EMC 检测的准时率是多少", entities={"business_line": "emc"}))

    # 业务线是本轮明说的（不算继承），区域/时间是继承来的 —— 上层据此决定能不能放宽
    assert set(merged.inherited_dimensions) == {"metric", "region", "time"}


def test_inherit_filters_false_keeps_metric_only():
    """空结果回退用：只沿用指标（问题类型），不带入上一轮的过滤维度。"""
    ctx = QueryContext(metric=Metric("m", "检测准时率", "运营级", "通用", "x", "y"),
                       business_line="reliability", region="华东", time="上个月")
    relaxed = ctx.inherit(_mapped("EMC 检测的准时率是多少", entities={"business_line": "emc"}),
                          inherit_filters=False)

    assert relaxed.metric is not None                 # 指标仍沿用上一轮
    assert "region" not in relaxed.entities           # 过滤维度不带入
    assert "time" not in relaxed.entities
    assert relaxed.entities["business_line"] == "emc"  # 本轮明说的保留
    assert relaxed.inherited_dimensions == ["metric"]
    assert any("空结果回退" in r for r in relaxed.reasons)


def test_inherit_filters_false_still_keeps_group_by_of_this_turn():
    """本轮要分组时，放宽也不能把分组维度当成过滤条件塞进去。"""
    ctx = QueryContext(region="华东", time="上个月")
    relaxed = ctx.inherit(_mapped("各业务线的准时率", entities={"group_by": "business_line"}),
                          inherit_filters=False)

    assert "region" not in relaxed.entities
    assert relaxed.entities["group_by"] == "business_line"


# ---------------- 客户维度 + 上一轮问题/结果 ----------------

def test_inherit_fills_customer():
    ctx = QueryContext(customer="某汽车客户")
    merged = ctx.inherit(_mapped("那合同金额呢"))

    assert merged.entities["customer"] == "某汽车客户"
    assert "某汽车客户" in merged.normalized, "继承的客户名要进归一化问句，否则进不了生成提示词"
    assert merged.inherited_dimensions == ["customer"]


def test_grouping_by_customer_is_not_inherited_as_filter():
    """本轮要按客户分组时，不能再把上一轮的客户继承成过滤条件（否则只剩一行）。"""
    ctx = QueryContext(customer="某汽车客户")
    merged = ctx.inherit(_mapped("各客户的合同金额是多少", entities={"group_by": "customer"}))

    assert "customer" not in merged.entities
    assert any("分组优先" in r for r in merged.reasons)


def test_update_from_records_customer_and_last_question():
    ctx = QueryContext()
    ctx.update_from(_mapped(
        "某汽车客户的合同金额是多少",
        entities={"customer": "某汽车客户", "metric": "contract_amount"},
    ))

    assert ctx.customer == "某汽车客户"
    # 追问改写要用**原始问题**（含用户原话），不能用被继承补齐后的 normalized
    assert ctx.last_question == "某汽车客户的合同金额是多少"


def test_reset_clears_last_question_and_result():
    ctx = QueryContext()
    ctx.update_from(_mapped("某汽车客户的合同金额是多少", entities={"customer": "某汽车客户"}))
    ctx.note_result("2,540,000")

    ctx.reset()

    assert ctx.customer is None
    assert ctx.last_question == "" and ctx.last_result == ""


def test_empty_result_fallback_drops_customer_too():
    """空结果回退（inherit_filters=False）必须把客户也一起放开，否则回退形同虚设。"""
    ctx = QueryContext(customer="某汽车客户", region="华东")
    relaxed = ctx.inherit(_mapped("合同金额是多少"), inherit_filters=False)

    assert "customer" not in relaxed.entities
    assert "region" not in relaxed.entities
