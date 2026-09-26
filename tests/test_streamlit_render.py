"""用官方 `streamlit.testing.v1.AppTest` 做**真实渲染**测试。

为什么还需要这一层（已有假 streamlit 的入口冒烟测试）：
    假替身只能看到"调用了哪些 widget"，看不到 Streamlit 自己的**执行顺序**语义。
    本轮「对话区示例问题」就踩了一个只有真实渲染才暴露的坑：
      · 示例块若放在 `st.chat_input` **之后** —— 按钮返回值要到脚本末尾才产生，
        于是点了示例当轮不提问，用户感觉"点了没反应"（要再操作一次才问）；
      · 示例块放在**之前**则相反：点击当轮 turns 还是空的，示例会与刚问出的答案同屏出现。
    最终解是「放在之前 + 问完 st.rerun() 一次」，这两条不变量只有 AppTest 能证实，
    因此这里的测试专门盯它们（LLM 引擎被替换成假返回，测试不联网、不调模型）。
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

pytest.importorskip("streamlit.testing.v1")
from streamlit.testing.v1 import AppTest  # noqa: E402

from nl2sql.models import GenerationResult, ResultSource  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "streamlit_app.py"

SQL_LABEL = "🔢 SQL · PostgreSQL"
ES_LABEL = "🔎 DSL · Elasticsearch"


def _samples(at) -> list[str]:
    return [b.label for b in at.button if b.key and b.key.startswith("sample_")]


@pytest.fixture
def fake_sql_engine(monkeypatch):
    """把 GRGQueryEngine.ask 换成固定返回，避免测试里真调 LLM/真查库。"""
    from examples.grg_engine import GRGQueryEngine

    def fake_ask(self, question, *a, **kw):
        return {
            "type": "result",
            "mapped": types.SimpleNamespace(metric=None, reasons=["（测试替身）"],
                                            entities={"region": "华东"}, normalized=question),
            "result": GenerationResult(sql="SELECT 42 AS answer", raw="SELECT 42 AS answer",
                                       source=ResultSource.LLM),
            "cols": ["answer"],
            "rows": [(42,)],
            "row_count": 1,
            "empty": False,
        }

    monkeypatch.setattr(GRGQueryEngine, "ask", fake_ask)
    return fake_ask


def test_app_renders_without_exception_and_shows_three_samples(fake_sql_engine):
    """应用能在真实 Streamlit 运行时正常渲染，且新对话给出 3 条示例。"""
    at = AppTest.from_file(str(ENTRY), default_timeout=120)
    at.run()

    assert not at.exception, [e.value for e in at.exception]
    assert len(_samples(at)) == 3


def test_clicking_a_sample_asks_immediately_and_hides_the_samples(fake_sql_engine):
    """点示例：**当轮就提问**（不是等下次交互），并且示例区同轮消失。

    这两条正是"位置放错"时会坏掉的地方：
      · 放到 chat_input 之后 -> 当轮 turns 为空（点了没反应）；
      · 不重跑             -> 示例与刚问出的答案同屏。
    """
    at = AppTest.from_file(str(ENTRY), default_timeout=120)
    at.run()
    assert _samples(at), "前置条件：新对话应有示例"

    at.button(key="sample_sql_0").click().run()

    turns = at.session_state["turns_by_engine"]["sql"]
    assert not at.exception, [e.value for e in at.exception]
    assert len(turns) == 2, f"点示例后应当轮完成一问一答，实际 {turns}"
    assert turns[0]["content"] == "华东区上个月可靠性试验的准时完成率是多少"
    assert turns[-1]["role"] == "assistant"
    assert _samples(at) == [], "第一轮完成后示例区应消失"


def test_samples_come_back_after_clearing_the_conversation(fake_sql_engine):
    """清空当前档对话（= 新对话）后，示例重新出现。"""
    at = AppTest.from_file(str(ENTRY), default_timeout=120)
    at.run()
    at.button(key="sample_sql_0").click().run()
    assert _samples(at) == []

    [b for b in at.button if b.label and "清空" in b.label][0].click().run()

    assert at.session_state["turns_by_engine"]["sql"] == []
    assert len(_samples(at)) == 3


def test_each_engine_renders_its_own_three_samples(fake_sql_engine):
    """三档各自渲染 3 条**本档**示例（切档不会串）。"""
    for label, mode in ((SQL_LABEL, "sql"), (ES_LABEL, "es")):
        at = AppTest.from_file(str(ENTRY), default_timeout=120)
        at.run()
        at.radio(key="engine_label_v4").set_value(label).run()

        shown = _samples(at)
        assert not at.exception, (mode, [e.value for e in at.exception])
        assert len(shown) == 3, (mode, shown)
        # 示例必须与当前档匹配：SQL 档问业务指标，日志档问事件流
        if mode == "sql":
            assert any("准时" in s or "通过率" in s or "17025" in s for s in shown), shown
        else:
            assert all("告警" in s or "ERROR" in s or "日志" in s for s in shown), shown


def test_switching_engine_shows_that_engines_own_history(fake_sql_engine):
    """切档只看到该档自己的对话（历史隔离在真实运行时也成立）。"""
    at = AppTest.from_file(str(ENTRY), default_timeout=120)
    at.run()
    at.button(key="sample_sql_0").click().run()
    assert len(at.session_state["turns_by_engine"]["sql"]) == 2

    at.radio(key="engine_label_v4").set_value(ES_LABEL).run()

    assert at.session_state["turns_by_engine"]["es"] == []
    assert len(_samples(at)) == 3, "ES 档是它自己的新对话，应展示示例"


def test_context_fallback_is_disclosed_in_the_ui(monkeypatch):
    """空结果回退必须在界面上讲清楚（否则用户会以为口径被悄悄改了）。"""
    from examples.grg_engine import GRGQueryEngine

    def fake_ask(self, question, *a, **kw):
        return {
            "type": "result",
            "mapped": types.SimpleNamespace(
                metric=None, reasons=["空结果回退: 不使用上一轮继承的过滤维度（区域/业务线/时间）"],
                entities={"business_line": "emc"}, normalized=question,
                inherited_dimensions=["metric"],
            ),
            "result": GenerationResult(sql="SELECT 0.75 AS on_time_rate", raw="s",
                                       source=ResultSource.LLM),
            "cols": ["on_time_rate"],
            "rows": [(0.75,)],
            "row_count": 1,
            "empty": False,
            "context_fallback": {"dropped": ["region", "time"], "normalized": question},
        }

    monkeypatch.setattr(GRGQueryEngine, "ask", fake_ask)
    at = AppTest.from_file(str(ENTRY), default_timeout=120)
    at.run()
    at.button(key="sample_sql_0").click().run()

    assert not at.exception, [e.value for e in at.exception]
    warnings = " ".join(w.value for w in at.warning)
    assert "已忽略这些继承维度重新查询" in warnings
    assert "区域" in warnings and "时间" in warnings      # 维度用中文讲清楚，不暴露内部键名
