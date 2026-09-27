"""口径一致性校验（nl2sql/caliber.py）测试。

这一层只做一件事：**看生成的 SQL 有没有按语义层的口径来**（提示级约束模型可以不遵守）。
判定原则是"宁可漏报，不可误报"——它是提示级校验，误报会把真问题淹没在噪声里。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from examples.grg_engine import GRGQueryEngine
from examples.grg_schema import build_metric_guards, build_registry, build_semantic_layer, build_store
from nl2sql.caliber import check_caliber, check_mapped
from nl2sql.config import Settings
from nl2sql.models import ResultSource
from nl2sql.pipeline import Text2SQLPipeline
from tests.doubles import GRGMockLLM, GRGSampleDB

LAYER = build_semantic_layer()
GUARD = LAYER.metric_guards["contract_amount"]
GROUP_COLUMNS = LAYER.GROUP_COLUMNS


def _check(sql, *, entities=None, group_by=None, question="", guard=GUARD):
    return check_caliber(
        sql, entities=entities or {}, group_by=group_by, guard=guard,
        group_columns=GROUP_COLUMNS, question=question,
    )


# ---------------- ① 实体过滤必须落地 ----------------

def test_dropped_customer_filter_is_caught():
    """真机旧行为：客户名被整个丢掉 -> SELECT SUM(amount) FROM contracts（全库）。"""
    p = _check("SELECT SUM(amount) AS contract_amount FROM contracts",
               entities={"customer": "某汽车客户"}, question="某汽车客户的合同金额是多少")

    assert p and "某汽车客户" in p[0] and "过滤没落地" in p[0]


def test_landed_customer_filter_passes():
    p = _check("SELECT SUM(amount) FROM contracts c JOIN customers cu ON c.customer_id = cu.id "
               "WHERE cu.name = '某汽车客户'",
               entities={"customer": "某汽车客户"}, question="某汽车客户的合同金额是多少")

    assert p == []


def test_region_value_must_land_too():
    p = _check("SELECT COUNT(*) FROM reports", entities={"region": "华南"}, question="华南报告数")

    assert p and "华南" in p[0]


def test_business_line_is_not_checked():
    """业务线的 code 与中文名都算合法写法 —— 硬判必误报，所以不判（宁可漏报）。"""
    p = _check("SELECT COUNT(*) FROM reports r JOIN business_lines b ON r.business_line_id = b.id "
               "WHERE b.name = '可靠性与环境试验'",
               entities={"business_line": "reliability"}, question="可靠性报告数",
               guard=None)      # 本例只验「值检查」，不掺合同口径的禁表断言

    assert p == []


# ---------------- ② 分组维度必须输出展示列 ----------------

def test_group_by_customer_needs_name_column():
    """真机旧行为：`SELECT c.customer_id ... GROUP BY c.customer_id` —— 用户看不懂是哪家客户。"""
    p = _check("SELECT c.customer_id, SUM(c.amount) FROM contracts c GROUP BY c.customer_id",
               entities={"group_by": "customer"}, group_by="customer",
               question="各客户的合同金额是多少")

    assert p and "customers.name" in p[0]


def test_group_by_with_name_column_passes():
    p = _check("SELECT cu.name, SUM(ct.amount) FROM contracts ct "
               "JOIN customers cu ON ct.customer_id = cu.id GROUP BY cu.name",
               entities={"group_by": "customer"}, group_by="customer",
               question="各客户的合同金额是多少")

    assert p == []


def test_no_group_by_means_no_group_check():
    p = _check("SELECT SUM(amount) FROM contracts", entities={"metric": "contract_amount"})

    assert p == []


# ---------------- ③ 指标口径的硬约束（MetricGuard） ----------------

def test_missing_triggered_filter_is_caught():
    p = _check("SELECT cu.name FROM contracts ct JOIN customers cu ON ct.customer_id = cu.id "
               "GROUP BY cu.name ORDER BY SUM(ct.amount) DESC LIMIT 1",
               entities={"metric": "contract_amount"}, question="已开票合同金额最高的客户是哪个")

    assert p and "已开票" in p[0]


def test_filter_requirement_is_gated_by_question():
    """口径写的是"**可**按 settled_status 过滤" —— 问题没提已开票时不该要求它（否则就是误报）。"""
    p = _check("SELECT SUM(amount) FROM contracts",
               entities={"metric": "contract_amount"}, question="合同金额是多少")

    assert p == []


def test_forbidden_join_is_caught_regardless_of_question():
    """「该口径不得 JOIN 某表」是定义的一部分，与问法无关（不受触发词门控）。"""
    p = _check("SELECT SUM(ct.amount) FROM contracts ct JOIN reports r ON r.order_id = ct.id",
               entities={"metric": "contract_amount"}, question="合同金额是多少")

    assert p and "reports" in p[0]


# ---------------- 不误报的兜底 ----------------

def test_unparsable_or_empty_sql_is_not_flagged():
    """解析失败 / 空 SQL 交给静态校验层管，这里既不重复报错也不误报。"""
    assert _check("") == []
    assert _check("not sql at all !!!") == []
    assert _check("SELECT SUM(amount) FROM contracts", entities={}, guard=None) == []


# ---------------- 资产体检：口径断言不能和指标自己的 SQL 参考打架 ----------------

def test_every_metric_guard_accepts_its_own_sql_hint():
    """每个带 guard 的指标，它自己的 `sql_hint` 必须在**不含触发词**的问题下通过。

    否则会出现"指标口径与出口断言互相矛盾"：模型照着 hint 写也被判违规。
    """
    for mid, guard in build_metric_guards().items():
        metric = LAYER.metrics[mid]
        assert metric.sql_hint, f"{mid} 声明了 guard 却没有 sql_hint"
        problems = check_caliber(
            metric.sql_hint, entities={"metric": mid}, guard=guard,
            group_columns=GROUP_COLUMNS, question=f"{metric.name}是多少",
            dialect="postgres",
        )
        assert problems == [], f"{mid} 的 sql_hint 未能通过自己的口径断言: {problems}"


# ---------------- 引擎出口：违规要如实进 payload ----------------

def _engine_with_runner(sql: str):
    settings = Settings()
    registry = build_registry(settings.db.dialect)
    layer = build_semantic_layer()
    pipeline = Text2SQLPipeline(
        registry=registry, store=build_store(), llm=GRGMockLLM(),
        db=GRGSampleDB(registry, dialect=settings.db.dialect), graph=layer.graph,
    )

    class _Runner:
        """假编排层：直接返回指定 SQL 与行，用来构造"违规 SQL"场景。"""

        glossary = None
        doc_context = None
        guard = None

        def query(self, question, pre_feedback=None):
            res = SimpleNamespace(sql=sql, source=ResultSource.LLM, error=None)
            return res, ["contract_amount"], [(5450000,)]

    return GRGQueryEngine(pipeline, layer, runner=_Runner())


def test_engine_attaches_caliber_warning():
    out = _engine_with_runner("SELECT SUM(amount) AS contract_amount FROM contracts").ask(
        "某汽车客户的合同金额是多少"
    )

    assert out["type"] == "result"
    assert out["caliber"]["ok"] is False
    assert any("过滤没落地" in r for r in out["caliber"]["reasons"])


def test_engine_has_no_caliber_key_when_clean():
    out = _engine_with_runner(
        "SELECT SUM(amount) AS contract_amount FROM contracts c "
        "JOIN customers cu ON c.customer_id = cu.id WHERE cu.name = '某汽车客户'"
    ).ask("某汽车客户的合同金额是多少")

    assert "caliber" not in out
