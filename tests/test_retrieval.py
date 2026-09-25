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
