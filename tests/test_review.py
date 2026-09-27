"""答案评审层（档 1）单元测试：纯函数、离线可跑。

背景（真机感受）："它自以为地输出了一个自认合理的结果" —— RAG 答「资料中未涉及。」
本身没错（提示词就是这么要求的），但那说明**系统没答上**，却被当正常回答展示。
评审层负责把它降级为引导。
"""
from __future__ import annotations

from nl2sql.review import EXAMPLE_QUESTIONS, NO_ANSWER_MARK, guidance, review_answer
from nl2sql.vectorstore import DocHit


def _rag(answer: str) -> dict:
    return {
        "type": "rag",
        "mapped": "MAPPED",
        "answer": answer,
        "docs": [DocHit(id="k1", title="术语：EMC", content="正文")],
    }


def test_rag_no_answer_is_downgraded_to_guidance():
    out = review_answer(_rag(f"{NO_ANSWER_MARK}。"))

    assert NO_ANSWER_MARK not in out["answer"]
    assert "可以这样问" in out["answer"]
    assert out["review"]["verdict"] == "no_answer"
    assert out["review"]["reason"]


def test_guidance_lists_example_questions():
    """引导必须给出示范问法——只说"我不懂"对用户没有帮助。"""
    out = review_answer(_rag(f"{NO_ANSWER_MARK}。"))

    for q in EXAMPLE_QUESTIONS:
        assert q in out["answer"], f"引导里应有示范问法：{q}"


def test_type_docs_and_mapped_are_preserved():
    """评审只换答案措辞，不动类型、不丢引用来源。

    与结构化分支保持一致：执行成功但 0 行时仍是 `type=result` + `empty=True`，
    由界面给出警示，而不是改类型。这样调用方与评估集都不受影响。
    """
    original = _rag(f"{NO_ANSWER_MARK}。")
    out = review_answer(original)

    assert out["type"] == "rag"
    assert out["docs"] is original["docs"]
    assert out["mapped"] == original["mapped"]


def test_real_answer_is_left_untouched():
    out = _rag("EMC 是电磁兼容性检测的简称 [1]。")

    assert review_answer(out) == out
    assert "review" not in review_answer(out), "答上了就不该加评审标记"


def test_non_rag_payloads_are_untouched():
    """只评审 RAG 答案；结构化结果的空结果处理另有机制（空结果回退 + 界面警示）。"""
    for payload in (
        {"type": "result", "rows": [], "cols": []},
        {"type": "clarification", "message": "x"},
        {"type": "refused", "answer": "x"},
    ):
        assert review_answer(payload) == payload


def test_guidance_prefixes_the_reason():
    text = guidance("随便一个理由")

    assert text.startswith("随便一个理由")
    assert all(q in text for q in EXAMPLE_QUESTIONS)


def test_no_answer_mark_matches_the_prompt_contract():
    """兜底话术是提示词的**契约**：kb 的 RAG 提示词必须用到同一个常量，不能两处各写一份。"""
    from nl2sql.kb import RAG_SYSTEM_PROMPT

    assert NO_ANSWER_MARK in RAG_SYSTEM_PROMPT
