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


def test_knowledge_question_is_flagged_for_rag():
    """定义/解释/对照类问句标记 knowledge，供引擎优先路由到文档问答。"""
    m = SemanticMapper(_layer())
    assert m.map("EMC 是什么意思").entities.get("knowledge") is True
    assert m.map("ISO/IEC 17025 和 GB/T 27025 有什么区别").entities.get("knowledge") is True
    assert m.map("什么是检测准时率").entities.get("knowledge") is True


def test_data_question_is_not_flagged_as_knowledge():
    """「是多少」是数据问句，与「是什么」一字之差，不能被误判。"""
    m = SemanticMapper(_layer())
    assert "knowledge" not in m.map("华东区上个月的准时率是多少").entities
    assert "knowledge" not in m.map("各业务线的检测准时率是多少").entities


def test_knowledge_flag_coexists_with_metric():
    """知识型问句即使解析出指标也要带标记 —— 引擎据此覆盖"有指标就走 SQL"的判断。"""
    m = SemanticMapper(_layer())
    mapped = m.map("检测准时率是什么意思")
    assert mapped.metric is not None          # 指标照样解析出来（用于展示/权限）
    assert mapped.entities.get("knowledge") is True


# ---------------- 未识别槽位自检：不静默沿用上一轮的值 ----------------
# 真机：先问「华南呢」-> 0.8667；再问「那华西 准确率呢」-> 还是 0.8667。
# 「华西」不在区域词表、「准确率」不是任何指标的别名，两个槽位都空了，
# 而 inherit() 只补"缺失"的槽位 —— 在它眼里"用户没提"和"用户提了但没懂"是同一种情况，
# 于是双双沿用上一轮，用户拿到一个"看起来正常、但问的根本不是他要的"数字。


def test_unrecognized_region_asks_for_clarification():
    cl = SemanticMapper(_layer()).map("那华西 准确率呢").clarification

    assert cl and "华西" in cl
    assert "华东" in cl and "华南" in cl, "要给出可选区域，而不是只说'我没懂'"


def test_unrecognized_metric_word_asks_for_clarification():
    cl = SemanticMapper(_layer()).map("那准确率呢").clarification

    assert cl and "准确率" in cl
    assert "检测准时率" in cl, "要列出当前可用的指标"


def test_stopword_prefix_is_stripped_from_the_hint():
    """正则可能把虚词一起吃进来（"那准确率"）——提示语里必须还原成「准确率」。"""
    cl = SemanticMapper(_layer()).map("那准确率呢").clarification

    assert "「准确率」" in cl
    assert "那准确率" not in cl


def test_valid_values_are_never_blocked():
    """合法问法与多轮追问一律不能误拦——这是新规则最容易犯错的地方。"""
    m = SemanticMapper(_layer())
    for q in (
        "那华南区呢？",              # 有效区域
        "那华北呢",                  # 有效区域
        "华南的准时率是多少",         # 有效区域 + 有效指标
        "华东区上个月可靠性试验的准时完成率是多少",
        "各业务线的检测准时率是多少",
    ):
        assert m.map(q).clarification is None, f"误拦: {q}"


def test_ambiguous_synonym_clarification_takes_priority():
    """已存在歧义澄清时不被覆盖——那条附带消歧重写规则，信息更具体。"""
    cl = SemanticMapper(_layer()).map("那个做环境的华西实验室").clarification

    assert cl and "环境可靠性实验室" in cl
    assert "不是有效区域" not in cl
