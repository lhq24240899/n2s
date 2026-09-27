"""企业知识库 / 混合检索 / 路由 的单元测试（全离线：不联网、不碰真库）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nl2sql.config import Settings
from nl2sql.kb import build_sql_doc_block, rrf_fuse, tokenize_cn
from nl2sql.pipeline import Text2SQLPipeline
from nl2sql.vectorstore import DocHit

from examples.grg_engine import GRGQueryEngine
from examples.grg_schema import build_registry, build_semantic_layer, build_store
from tests.doubles import GRGMockLLM, GRGSampleDB


# ---------------- RRF 融合 ----------------

def test_rrf_fuse_prefers_consensus():
    """三路都排前面的文档应拿到最高分（RRF 只依赖名次，天然免调权重）。"""
    fused = rrf_fuse([["a", "b", "c"], ["a", "c", "b"], ["b", "a"]], k=60)
    ids = [i for i, _ in fused]
    assert ids[0] == "a"
    assert set(ids) == {"a", "b", "c"}
    scores = dict(fused)
    assert scores["a"] > scores["c"]


def test_rrf_fuse_single_list_keeps_order():
    assert [i for i, _ in rrf_fuse([["x", "y", "z"]])] == ["x", "y", "z"]


def test_rrf_fuse_empty():
    assert rrf_fuse([[], []]) == []


# ---------------- 中文分词（关键词路的输入） ----------------

def test_tokenize_cn_keeps_meaningful_terms():
    toks = tokenize_cn("华东区上个月可靠性试验的准时完成率是多少")
    assert any(t in toks for t in ("准时", "准时率"))
    assert any(t.startswith("可靠性") for t in toks)
    assert "华东" in toks


def test_tokenize_cn_filters_stop_chars_and_limits():
    toks = tokenize_cn("华东区上个月可靠性试验的准时完成率是多少")
    assert not any("的是" in t for t in toks)
    assert len(toks) <= 24
    assert tokenize_cn("") == []


# ---------------- 文档块拼装 ----------------

def test_build_sql_doc_block_lists_titles():
    docs = [DocHit(id="d1", title="指标口径：检测准时率", content="正文", source="手册")]
    block = build_sql_doc_block(docs)
    assert block.startswith("# 企业知识库摘录")
    assert "指标口径：检测准时率" in block
    assert build_sql_doc_block([]) == ""


# ---------------- 引擎路由（结构化问数 vs 文档问答） ----------------

DOCS = [
    DocHit(
        id="k1",
        title="术语：EMC（电磁兼容检测）",
        content="EMC 是电磁兼容……",
        source="《业务术语与口径库》",
    )
]


class _FakeRetriever:
    """替身：固定返回给定文档，并记录被调用次数与每次的入参。"""

    def __init__(self, docs):
        self.docs = docs
        self.calls = 0
        self.questions: list[str] = []

    def retrieve(self, question):
        self.calls += 1
        self.questions.append(question)
        return self.docs


def _engine(doc_retriever=None) -> GRGQueryEngine:
    settings = Settings()
    registry = build_registry(settings.db.dialect)
    pipeline = Text2SQLPipeline(
        registry=registry,
        store=build_store(),
        llm=GRGMockLLM(),
        db=GRGSampleDB(registry, dialect=settings.db.dialect),
        top_k=settings.retrieval.top_k,
        min_score=settings.retrieval.min_score,
        max_retry=settings.pipeline.max_retry,
    )
    return GRGQueryEngine(pipeline, build_semantic_layer(), doc_retriever=doc_retriever)


def test_doc_question_routes_to_rag():
    """解析不出指标的文档类问题 -> 走知识库问答，并带出引用文档。"""
    retriever = _FakeRetriever(DOCS)
    out = _engine(retriever).ask("EMC 是什么意思")
    assert out["type"] == "rag"
    assert out["docs"] == DOCS
    assert out["answer"]
    assert retriever.calls == 1


def test_metric_question_routes_to_sql_and_injects_docs():
    """有指标 -> 走 SQL；知识库摘录被注入生成 prompt（混合 RAG 的另一半）。"""
    retriever = _FakeRetriever(DOCS)
    engine = _engine(retriever)
    out = engine.ask("华东区上个月可靠性试验的准时完成率是多少")
    assert out["type"] == "result"
    assert retriever.calls == 1
    assert engine.pipeline.doc_context
    assert "术语：EMC" in engine.pipeline.doc_context


def test_engine_without_retriever_degrades_to_pure_sql():
    """未配置知识库时不影响主流程，仍然能出结果。"""
    out = _engine(None).ask("华东区上个月可靠性试验的准时完成率是多少")
    assert out["type"] == "result"
    assert out["docs"] == []


def test_no_docs_means_clarify_instead_of_guessing():
    """检索为空时**如实澄清**，不再落到 SQL 去猜。

    期望值由 `type=="result"` 改为 `clarification`：原先那条"落到 SQL"的降级路径，
    在零相关资料时只会生成一张无意义的表（真机踩过：问「全部数据」被倒出
    equipment 整张表，界面还显示「✅ LLM 生成 · 通过静态校验 + 执行预检」，
    比报错更误导）。原来的意图——"别因为知识库不可用就把应用搞崩"——依然成立。
    """
    out = _engine(_FakeRetriever([])).ask("EMC 是什么意思")
    assert out["type"] == "clarification"
    assert "知识库" in out["message"]


# ---------------- 零意图问句：不许瞎猜 ----------------
# 真机踩过：问「全部数据」-> 倒出 equipment 整张表。链路是——
# 语义映射零信号 -> 路由进 SQL -> Schema Linking 推不出候选表、退化成全部 9 张表
# -> 示例检索 0 命中（无 few-shot）-> LLM 收到"一句废话 + 9 张表结构"只能瞎猜。

def test_zero_intent_question_asks_for_clarification():
    retriever = _FakeRetriever([])
    out = _engine(retriever).ask("全部数据")
    assert out["type"] == "clarification", f"应澄清，实际 {out.get('type')}"
    assert "指标" in out["message"]
    # 澄清发生在生成之前：不能有任何查询产物
    assert "rows" not in out and "result" not in out, "澄清不该产生查询结果"


def test_zero_intent_with_docs_goes_to_rag():
    """零意图但知识库有命中 -> 交给文档问答，而不是澄清。"""
    out = _engine(_FakeRetriever(DOCS)).ask("全部数据")
    assert out["type"] == "rag"


def test_follow_up_without_local_intent_still_goes_to_sql():
    """多轮追问本身没有意图（"那华南区呢？"），必须靠**继承的指标**保住结构化路由。

    这是新规则最容易误伤的地方：判据要用继承后的 merged，不能用本轮 parsed，
    否则 F02/F03/F05 这类多轮用例会被误拦成澄清。
    """
    engine = _engine(_FakeRetriever([]))
    first = engine.ask("华东区上个月可靠性试验的准时完成率是多少")
    assert first["type"] == "result", "前置条件：第一轮应把指标存进上下文"

    out = engine.ask("那华南区呢？")
    assert out["type"] == "result", f"合法追问被误拦成 {out.get('type')}"


def test_definition_question_after_metric_question_still_routes_to_rag():
    """多轮回归：先问指标问题，再问「EMC 是什么意思」，第二次必须**仍走 RAG**。

    真机 bug（用户报的）：上一轮的指标连同 region/time 被 inherit() 回填进 merged，
    而路由判据是 `merged.metric is not None` —— 于是定义类问题被判成结构化问句，
    拿"检测准时率 WHERE 业务线=emc AND 区域=华南 AND 上个月"去查库，
    页面显示「查询已执行成功，但无匹配数据」，还会白白多跑一次空结果回退（耗时翻倍）。

    定义类问题与上一轮毫无关系，不该被继承来的指标劫持。
    """
    retriever = _FakeRetriever(DOCS)
    engine = _engine(retriever)

    first = engine.ask("华东区上个月可靠性试验的准时完成率是多少")
    assert first["type"] == "result", "前置条件：第一轮应正常出结果并把指标存进上下文"

    out = engine.ask("EMC 是什么意思")
    assert out["type"] == "rag", f"定义类问题被继承的指标劫持成了 {out.get('type')}"
    assert out["docs"] == DOCS
    assert out["answer"]


def test_knowledge_question_is_not_polluted_by_inherited_filters():
    """被劫持时还会顺带继承 region/time，把定义问题变成一次带过滤条件的查询。

    修好后应确认：文档检索用的是**本轮问题**，上一轮继承的维度不参与。
    （注意：`normalized` 里出现「电磁兼容检测」是**本轮**同义词展开，属正常行为；
    不该出现的是上一轮的 华东/上个月/reliability。）
    """
    retriever = _FakeRetriever(DOCS)
    engine = _engine(retriever)

    engine.ask("华东区上个月可靠性试验的准时完成率是多少")
    engine.ask("EMC 是什么意思")

    q = retriever.questions[-1]
    assert q.startswith("EMC 是什么意思"), q      # 本轮原问题打头
    assert "华东" not in q, f"上一轮继承的区域污染了文档检索: {q}"
    assert "上个月" not in q, f"上一轮继承的时间污染了文档检索: {q}"
    assert "reliability" not in q, f"上一轮继承的业务线污染了文档检索: {q}"
