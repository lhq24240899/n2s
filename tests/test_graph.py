"""LangGraph 编排的离线测试（用 tests/doubles 的替身，不联网、不碰真库）。

覆盖三件事：
1. 正常路径：retrieve → link → generate → validate → execute → critique → END；
2. **Critic 反思闭环**：结果为空被规则打回 → 带反馈重生成 → 通过；
3. 额度耗尽：重试用完 → fallback，且流程轨迹里能看到每一步。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nl2sql.config import Settings
from nl2sql.db import DBRunner
from nl2sql.graph import Text2SQLGraph
from nl2sql.models import ResultSource
from nl2sql.policy import DataPolicy, PolicyGuard, RowFilter

from examples.grg_schema import build_registry, build_store
from tests.doubles import GRGMockLLM, GRGSampleDB

QUESTION = "华东区上个月可靠性试验的准时完成率是多少"


class _ScriptedDB(DBRunner):
    """按脚本返回执行结果：前 empty_times 次返回全 NULL，之后返回正常值。"""

    def __init__(self, empty_times: int, value=0.88):
        self.empty_times = empty_times
        self.value = value
        self.calls = 0

    def explain(self, sql):
        return True, None

    def execute(self, sql):
        self.calls += 1
        if self.calls <= self.empty_times:
            return (["on_time_rate"], [(None,)])
        return (["on_time_rate"], [(self.value,)])


def _graph(db=None, max_retry=1) -> Text2SQLGraph:
    settings = Settings()
    registry = build_registry(settings.db.dialect)
    return Text2SQLGraph(
        registry=registry,
        store=build_store(),
        llm=GRGMockLLM(),
        db=db or GRGSampleDB(registry, dialect=settings.db.dialect),
        top_k=settings.retrieval.top_k,
        min_score=settings.retrieval.min_score,
        max_retry=max_retry,
    )


def test_graph_happy_path_records_all_nodes():
    res = _graph().run(QUESTION)
    assert res.source == ResultSource.LLM
    assert res.sql
    assert res.cols == ["on_time_rate"]
    steps = " ".join(res.graph_steps)
    for node in ("retrieve", "link", "generate", "validate", "execute", "critique"):
        assert node in steps, f"流程轨迹应包含 {node}"


def test_critique_rejects_empty_then_recovers():
    """Critic 打回 -> 带反馈重生成 -> 第二次拿到数据，最终 source 仍是 llm。"""
    db = _ScriptedDB(empty_times=1)
    res = _graph(db=db).run(QUESTION)
    steps = " ".join(res.graph_steps)
    assert "critique：结果为空 → 要求修正" in steps
    assert "带错误反馈重生成" in steps
    assert res.rows == [(0.88,)]
    assert res.source == ResultSource.LLM
    assert db.calls == 2


def test_gives_up_and_falls_back_when_budget_exhausted():
    """一直为空：重试用完就走 fallback，不会无限循环。"""
    db = _ScriptedDB(empty_times=99)
    res = _graph(db=db, max_retry=1).run(QUESTION)
    steps = " ".join(res.graph_steps)
    assert "fallback" in steps
    assert res.source == ResultSource.FALLBACK_TEMPLATE
    # max_retry=1 -> 最多 2 次生成（首次 + 1 次重试），另加 fallback 一次执行
    assert "generate：第 2 次生成" in steps
    assert "generate：第 3 次生成" not in steps


def test_validation_error_triggers_feedback_retry():
    """校验层拦下（幻觉字段）后应带错误反馈重生成，而不是直接结束。"""
    from tests.doubles import MockLLM

    class _Hallucinating(MockLLM):
        def generate(self, prompt, system=None):
            return "SELECT SUM(amount) AS gmv FROM orders"

    settings = Settings()
    registry = build_registry(settings.db.dialect)
    graph = Text2SQLGraph(
        registry=registry,
        store=build_store(),
        llm=_Hallucinating(),
        db=GRGSampleDB(registry, dialect=settings.db.dialect),
        max_retry=1,
    )
    res = graph.run("昨天GMV是多少")
    steps = " ".join(res.graph_steps)
    assert "validate：拦截" in steps
    assert "带错误反馈重生成" in steps
    assert res.source in (ResultSource.FALLBACK_TEMPLATE, ResultSource.LLM)


def test_mermaid_export_available():
    mermaid = _graph().mermaid()
    assert "retrieve" in mermaid and "critique" in mermaid and "-->" in mermaid


def test_llm_critic_disabled_by_default():
    res = _graph().run(QUESTION)
    assert res.critique == "规则检查通过"


# ---------------------------------------------------------------------------
# 数据权限：两条编排必须表现一致（换编排不能换安全等级）
# ---------------------------------------------------------------------------

GROUP_Q = "各实验室设备利用率"


def test_graph_applies_row_level_filter():
    guard = PolicyGuard(
        DataPolicy(row_filters=(RowFilter("labs", "region", ("华东",)),), dialect="postgres")
    )
    g = _graph()
    g.guard = guard
    res = g.run(GROUP_Q)
    assert "l.region = '华东'" in res.sql
    assert any("已注入行级过滤" in n for n in guard.applied)


def test_graph_blocks_unauthorized_column():
    guard = PolicyGuard(DataPolicy(denied_columns=frozenset({"amount"}), dialect="postgres"))
    g = _graph()
    g.guard = guard
    res = g.run("华东区上个月可靠性试验的检测服务收入是多少")
    # 触及被禁字段的 SQL 不应被放行执行（要么重生成成功，要么回退且不带越权 SQL）
    assert res.sql is None or "amount" not in res.sql.lower()


def test_graph_runner_swaps_orchestration_without_touching_engine():
    """GraphRunner 让引擎在「手写 pipeline / LangGraph 图」之间切换，引擎代码零改动。"""
    from nl2sql.graph import GraphRunner
    from nl2sql.pipeline import Text2SQLPipeline

    from examples.grg_engine import GRGQueryEngine
    from examples.grg_schema import build_semantic_layer

    settings = Settings()
    registry = build_registry(settings.db.dialect)
    store = build_store()
    guard = PolicyGuard(
        DataPolicy(row_filters=(RowFilter("labs", "region", ("华东",)),), dialect="postgres")
    )
    pipeline = Text2SQLPipeline(
        registry=registry, store=store, llm=GRGMockLLM(),
        db=GRGSampleDB(registry, dialect=settings.db.dialect),
        top_k=settings.retrieval.top_k, min_score=settings.retrieval.min_score, max_retry=1,
    )
    pipeline.guard = guard
    engine = GRGQueryEngine(
        pipeline, build_semantic_layer(), guard=guard, runner=GraphRunner(_graph())
    )
    out = engine.ask(GROUP_Q)
    assert out["type"] == "result"
    assert "l.region = '华东'" in out["result"].sql  # 行级权限在图编排下同样生效
    assert out["rows"][0][0] == 0.81                 # 华东设备利用率（替身固定值）
