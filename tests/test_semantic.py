"""语义层单元测试：同义词解析、歧义澄清、指标解析、区域实体抽取。"""
from __future__ import annotations

from nl2sql.semantic import (
    Metric,
    MetricLevel,
    SemanticLayer,
    SemanticMapper,
    Synonym,
)


def _layer() -> SemanticLayer:
    metrics = [
        Metric(
            id="on_time_completion_rate",
            name="检测准时率",
            level=MetricLevel.OPERATION,
            domain="通用",
            definition="按期出具报告数 / 总报告数",
            sql_hint="SELECT ...",
            aliases=["准时完成率", "准时率"],
        ),
    ]
    synonyms = [
        Synonym("可靠性", "可靠性与环境试验", "business_line", "reliability"),
        Synonym("EMC", "电磁兼容检测", "business_line", "emc"),
        Synonym(
            "那个做环境的",
            "环境可靠性实验室",
            "business_line",
            "",
            ambiguous=True,
            clarification="您说的'那个做环境的'是指环境可靠性实验室吗？",
        ),
    ]
    return SemanticLayer(metrics=metrics, synonyms=synonyms, graph=None)


def test_synonym_expand():
    m = SemanticMapper(_layer())
    out = m.map("华东区可靠性试验的准时完成率")
    assert "可靠性与环境试验" in out.normalized
    assert out.entities.get("business_line") == "reliability"
    assert out.entities.get("region") == "华东"


def test_metric_resolution_by_alias():
    m = SemanticMapper(_layer())
    out = m.map("上个月准时完成率是多少")
    assert out.metric is not None
    assert out.metric.id == "on_time_completion_rate"


def test_ambiguity_clarification():
    m = SemanticMapper(_layer())
    out = m.map("那个做环境的实验室利用率怎么样")
    assert out.clarification is not None
    assert "环境可靠性实验室" in out.clarification


def test_no_false_substring():
    # "EMC" 不应误匹配到不含该词的句子
    m = SemanticMapper(_layer())
    out = m.map("计量服务收入是多少")
    assert "EMC" not in [s.split("->")[0] for s in out.resolved_synonyms]


def test_group_by_detection():
    """「各业务线」= 分组维度（GROUP BY），不是普通问句。"""
    m = SemanticMapper(_layer())
    out = m.map("各业务线的检测准时率是多少")
    assert out.entities.get("group_by") == "business_line"
    assert any("分组维度" in r for r in out.reasons)


def test_group_by_variants_and_no_false_positive():
    m = SemanticMapper(_layer())
    assert m.map("各实验室的准时率").entities.get("group_by") == "lab"
    assert m.map("按区域看准时率").entities.get("group_by") == "region"
    # 不含分组触发词的普通问句不应误判
    assert "group_by" not in m.map("华东区可靠性试验的准时完成率").entities


def test_group_by_glossary_instruction_and_same_dim_filter_skipped():
    layer = _layer()
    gl = layer.glossary_for(
        "on_time_completion_rate",
        {"group_by": "business_line", "business_line": "reliability", "region": "华东"},
    )
    text = gl.render()
    # 下发分组指令，且要求每行一个取值
    assert "GROUP BY" in text and "business_lines.name" in text
    # 正在分组的维度不能再当过滤条件
    assert "business_lines.code = 'reliability'" not in text
    # 其他维度照常下发过滤
    assert "labs.region = '华东'" in text
