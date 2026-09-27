"""追问改写（nl2sql/condense.py）的纯函数测试。

这一层的契约只有三句：
  1. 只在**信号弱**的追问上触发（有指标/分组/计数/排名/任一维度取值都不触发）；
  2. 改写结果必须清洗干净（去前缀引号、限长、挡住跑偏的 SQL）；
  3. 任何异常/不可用 -> 返回 None，由调用方保持原行为。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from examples.grg_schema import build_semantic_layer
from nl2sql.condense import (
    build_condense_prompt,
    clean_condensed,
    condense_followup,
    is_weak_followup,
)
from nl2sql.semantic import SemanticMapper


def _map(q: str):
    return SemanticMapper(build_semantic_layer()).map(q)


def test_weak_followup_detected():
    """「金额是多少」这种纯指代追问：无指标、无维度 -> 弱信号。"""
    assert is_weak_followup(_map("金额是多少")) is True
    assert is_weak_followup(_map("那它呢")) is True


def test_full_question_is_not_weak():
    """说清了指标/维度的一律不改写——改写只补指代，不做别的。"""
    m = SemanticMapper(build_semantic_layer())
    for q in (
        "华东区上个月可靠性试验的准时完成率是多少",   # 指标 + 区域 + 时间
        "各业务线的检测准时率是多少",                 # 指标 + 分组
        "一共出具了多少份报告",                       # 计数意图
        "EMC 是什么意思",                             # 知识型
    ):
        assert is_weak_followup(m.map(q)) is False, f"不该判为弱信号: {q}"


def test_prompt_carries_previous_turn_and_result():
    p = build_condense_prompt("已开票合同金额最高的客户是哪个", "金额是多少", "某汽车客户")
    assert "已开票合同金额最高的客户是哪个" in p
    assert "某汽车客户" in p
    assert "金额是多少" in p
    assert "# 任务" in p


def test_clean_strips_prefix_and_quotes():
    assert clean_condensed("改写后的问题：某汽车客户的合同金额是多少") == "某汽车客户的合同金额是多少"
    assert clean_condensed('"某汽车客户的合同金额是多少"') == "某汽车客户的合同金额是多少"
    assert clean_condensed("某汽车客户的合同金额是多少\n（说明：补上了指代）") \
        == "某汽车客户的合同金额是多少"


def test_clean_rejects_unusable_output():
    """空 / 超长 / 看起来是 SQL —— 一律判不可用（调用方保持原行为）。"""
    assert clean_condensed("") == ""
    assert clean_condensed("   ") == ""
    assert clean_condensed("x" * 61) == ""
    assert clean_condensed("SELECT SUM(amount) FROM contracts") == ""


class _StubLLM:
    def __init__(self, out: str | Exception):
        self.out = out

    def generate(self, prompt: str, system: str | None = None) -> str:
        if isinstance(self.out, Exception):
            raise self.out
        return self.out


def test_condense_followup_returns_rewritten_question():
    out = condense_followup(_StubLLM("某汽车客户的合同金额是多少"),
                            "已开票合同金额最高的客户是哪个", "金额是多少")
    assert out == "某汽车客户的合同金额是多少"


def test_condense_followup_swallows_llm_failure():
    """改写是增强项：模型调用失败必须静默降级（返回 None），绝不影响主流程。"""
    assert condense_followup(_StubLLM(RuntimeError("boom")), "上一轮问题", "金额是多少") is None
    assert condense_followup(_StubLLM("SELECT 1 FROM contracts"), "上一轮问题", "金额是多少") is None
