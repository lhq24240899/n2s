from tests.doubles import MockDBRunner, MockLLM
from nl2sql.db import DBRunner
from nl2sql.llm import LLMClient
from nl2sql.models import ResultSource
from nl2sql.pipeline import Text2SQLPipeline

from examples.schema import build_glossary, build_registry, build_store


def _pipeline(registry, store, glossary):
    return Text2SQLPipeline(
        registry, store, MockLLM(), MockDBRunner(registry), glossary, max_retry=1
    )


def test_llm_success():
    reg, store, gl = build_registry(), build_store(), build_glossary()
    res, cols, rows = _pipeline(reg, store, gl).query("昨天有多少订单")
    assert res.source == ResultSource.LLM
    assert res.sql is not None
    assert cols and rows


def test_fallback_template_on_hallucinated_column():
    reg, store, gl = build_registry(), build_store(), build_glossary()
    res, _, _ = _pipeline(reg, store, gl).query("昨天GMV是多少")
    # MockLLM 故意返回 SUM(amount)（幻觉列），被校验拦下 -> 回退到示例(用 total_amount)
    assert res.source == ResultSource.FALLBACK_TEMPLATE
    assert "total_amount" in res.sql
    assert res.error is not None  # 上游失败原因被保留


def test_fallback_generic():
    reg, store, gl = build_registry(), build_store(), build_glossary()

    class Failing(MockLLM):
        def generate(self, prompt, system=None):
            return "SELECT nope FROM ghost"

    p = Text2SQLPipeline(reg, store, Failing(), MockDBRunner(reg), gl, max_retry=1)
    res, _, _ = p.query("完全没有匹配的奇怪问题 qqq")
    assert res.source == ResultSource.FALLBACK_GENERIC


class _CountingLLM(LLMClient):
    def __init__(self):
        self.calls = 0

    def generate(self, prompt, system=None):
        self.calls += 1
        return "SELECT COUNT(*) AS cnt FROM orders"


class _ScriptedDB(DBRunner):
    """按脚本返回执行结果，用于模拟「先空后非空」。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def explain(self, sql):
        return True, None

    def execute(self, sql):
        r = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return r


def test_retry_on_empty_result():
    """SQL 能执行但返回全 NULL -> 应带反馈重生成一次（空结果自愈）。"""
    reg, store, gl = build_registry(), build_store(), build_glossary()
    llm = _CountingLLM()
    db = _ScriptedDB([(["cnt"], [(None,)]), (["cnt"], [(42,)])])
    p = Text2SQLPipeline(reg, store, llm, db, gl, max_retry=1)

    res, cols, rows = p.query("昨天有多少订单")

    assert llm.calls == 2, "应恰好重生成一次"
    assert rows == [(42,)]
    assert cols == ["cnt"]


def test_no_retry_when_result_not_empty():
    """结果非空时不应触发自愈，避免白白多花一次调用。"""
    reg, store, gl = build_registry(), build_store(), build_glossary()
    llm = _CountingLLM()
    db = _ScriptedDB([(["cnt"], [(7,)])])
    p = Text2SQLPipeline(reg, store, llm, db, gl, max_retry=1)

    _, _, rows = p.query("昨天有多少订单")

    assert llm.calls == 1
    assert rows == [(7,)]


def test_retry_on_empty_can_be_disabled():
    reg, store, gl = build_registry(), build_store(), build_glossary()
    llm = _CountingLLM()
    db = _ScriptedDB([(["cnt"], [(None,)]), (["cnt"], [(42,)])])
    p = Text2SQLPipeline(reg, store, llm, db, gl, max_retry=1, retry_on_empty=False)

    _, _, rows = p.query("昨天有多少订单")

    assert llm.calls == 1
    assert rows == [(None,)]


def test_is_empty_detection():
    assert Text2SQLPipeline._is_empty(["c"], []) is True
    assert Text2SQLPipeline._is_empty(["c"], [(None,), (None,)]) is True
    assert Text2SQLPipeline._is_empty(["c"], [(None,), (1,)]) is False
    assert Text2SQLPipeline._is_empty(["c"], [(0,)]) is False
