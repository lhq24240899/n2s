from tests.doubles import MockDBRunner, MockLLM
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
