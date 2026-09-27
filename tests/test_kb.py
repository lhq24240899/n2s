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


def test_no_docs_means_no_rag_fallback_to_sql():
    """检索为空时不强行走 RAG，退回原有 SQL 路径（不改变老行为）。"""
    out = _engine(_FakeRetriever([])).ask("EMC 是什么意思")
    assert out["type"] == "result"


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
