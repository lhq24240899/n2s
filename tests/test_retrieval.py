from nl2sql.retrieval import RetrievalService

from examples.schema import build_store


def test_tag_hit_and_reasons():
    svc = RetrievalService(build_store(), min_score=1.0)
    hits = svc.retrieve("昨天GMV是多少")
    ids = [h.example.id for h in hits]
    assert "ex_gmv" in ids
    gmv = next(h for h in hits if h.example.id == "ex_gmv")
    assert any("GMV" in r for r in gmv.reasons)
    assert any("昨天" in r for r in gmv.reasons)


def test_min_score_filters():
    svc = RetrievalService(build_store(), min_score=100.0)
    assert svc.retrieve("无关问题 xx") == []


def test_topk_limit():
    svc = RetrievalService(build_store(), min_score=0.0)
    hits = svc.retrieve("订单 用户 前 top", top_k=1)
    assert len(hits) <= 1


class _FakeVectorIndex:
    """替身：固定返回给定 (示例id, 相似度) 列表，模拟 pgvector 召回。"""

    def __init__(self, pairs):
        self.pairs = pairs
        self.calls = 0

    def search(self, embedding, top_k=10):
        self.calls += 1
        return self.pairs[:top_k]


class _FakeEmbedder:
    dim = 4

    def embed(self, texts):
        return [[0.0] * 4 for _ in texts]

    def embed_one(self, text):
        return [0.0] * 4


def test_hybrid_merges_tag_and_vector_with_reasons():
    """两路召回：标签分 + 向量相似度都要出现在 reasons 里（可解释性不丢）。"""
    store = build_store()
    # 向量路把 ex_topn（标签完全命不中）也带进候选，验证融合而非互相覆盖
    svc = RetrievalService(
        store,
        min_score=1.0,
        vector_index=_FakeVectorIndex([("ex_topn", 0.91), ("ex_gmv", 0.35)]),
        embedder=_FakeEmbedder(),
        vector_min_sim=0.30,
    )
    hits = svc.retrieve("昨天GMV是多少", top_k=3)
    ids = [h.example.id for h in hits]
    assert "ex_gmv" in ids and "ex_topn" in ids
    topn_hit = next(h for h in hits if h.example.id == "ex_topn")
    assert any("向量相似度" in r for r in topn_hit.reasons)
    assert any("RRF 融合得分" in r for r in topn_hit.reasons)
    gmv_hit = next(h for h in hits if h.example.id == "ex_gmv")
    assert any(r.startswith("标签命中") for r in gmv_hit.reasons)


def test_hybrid_vector_min_sim_filters_low_similarity():
    """相似度低于下限的向量召回不进入融合，避免引入无关范例。"""
    svc = RetrievalService(
        build_store(),
        min_score=1.0,
        vector_index=_FakeVectorIndex([("ex_topn", 0.05)]),
        embedder=_FakeEmbedder(),
        vector_min_sim=0.30,
    )
    assert [h.example.id for h in svc.retrieve("完全不相关问题zzz")] == []
