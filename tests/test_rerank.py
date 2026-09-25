"""rerank 精排的单元测试（用假 LLM，离线）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nl2sql.rerank import LLMReranker, NoopReranker, _parse_scores
from nl2sql.vectorstore import DocHit


def _docs(n: int) -> list[DocHit]:
    return [
        DocHit(id=f"d{i}", title=f"标题{i}", content="内容", source="", score=1.0, reasons=[f"RRF {i}"])
        for i in range(1, n + 1)
    ]


class _FakeLLM:
    def __init__(self, out: str):
        self.out = out
        self.calls = 0

    def generate(self, prompt, system=None):
        self.calls += 1
        return self.out


def test_parse_scores_variants():
    assert _parse_scores("1: 8\n2: 3", 2) == {1: 8.0, 2: 3.0}
    assert _parse_scores("1. 7分\n2、2", 2) == {1: 7.0, 2: 2.0}
    assert _parse_scores("[1] 9\n[2] 1", 2) == {1: 9.0, 2: 1.0}
    assert _parse_scores("抱歉我无法评分", 2) == {}
    # 越界编号忽略
    assert _parse_scores("9: 10", 2) == {}


def test_llm_rerank_reorders_and_truncates():
    """低分候选应被截断（避免把无关资料塞进 prompt）。"""
    docs = _docs(3)
    r = LLMReranker(_FakeLLM("1: 1\n2: 9\n3: 1"), min_score=3.0)
    out = r.rerank("q", docs, top_k=3)
    assert [d.id for d in out] == ["d2"]
    assert any("LLM 精排 9/10" in x for x in out[0].reasons)
    assert any("低分截断" in x for x in out[0].reasons)


def test_llm_rerank_keeps_best_when_all_below_threshold():
    docs = _docs(2)
    r = LLMReranker(_FakeLLM("1: 0\n2: 0"), min_score=3.0)
    out = r.rerank("q", docs, top_k=2)
    assert len(out) == 1, "全被截断时至少保留最高分那条"


def test_llm_rerank_falls_back_on_unparsable_output():
    """LLM 输出无法解析时不报错，保留融合顺序并在 reasons 里注明降级。"""
    docs = _docs(3)
    r = LLMReranker(_FakeLLM("我无法给出分数"), min_score=3.0)
    out = r.rerank("q", docs, top_k=2)
    assert [d.id for d in out] == ["d1", "d2"]
    assert any("解析失败" in x for x in out[0].reasons)


def test_llm_rerank_skips_call_for_single_doc():
    class _Boom:
        def generate(self, *a, **k):
            raise AssertionError("单条候选不应调用 LLM")

    assert LLMReranker(_Boom()).rerank("q", _docs(1), top_k=1)[0].id == "d1"


def test_noop_reranker_keeps_order():
    assert [d.id for d in NoopReranker().rerank("q", _docs(3), top_k=2)] == ["d1", "d2"]
