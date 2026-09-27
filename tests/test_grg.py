"""计量检测引擎端到端测试：单轮、多轮、歧义澄清。"""
from __future__ import annotations

import re

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nl2sql.config import Settings
from nl2sql.pipeline import Text2SQLPipeline

from examples.grg_engine import GRGQueryEngine
from examples.grg_schema import build_registry, build_semantic_layer, build_store
from tests.doubles import GRGMockLLM, GRGSampleDB
from nl2sql.llm import LLMClient


def _engine():
    settings = Settings()
    registry = build_registry(settings.db.dialect)
    store = build_store()
    layer = build_semantic_layer()
    llm = GRGMockLLM()
    db = GRGSampleDB(registry, dialect=settings.db.dialect)
    pipeline = Text2SQLPipeline(
        registry=registry, store=store, llm=llm, db=db,
        graph=layer.graph,   # 与生产装配一致：知识图谱参与 Schema Linking
        top_k=settings.retrieval.top_k, min_score=settings.retrieval.min_score,
        max_retry=settings.pipeline.max_retry,
    )
    return GRGQueryEngine(pipeline, layer)


def test_single_query_returns_sql():
    e = _engine()
    out = e.ask("华东区上个月可靠性试验的准时完成率是多少")
    assert out["type"] == "result"
    assert out["result"].sql
    assert out["result"].source.value in ("llm", "fallback_template", "fallback_generic")
    assert out["cols"] == ["on_time_rate"]


def test_multi_turn_changes_region():
    e = _engine()
    east = e.ask("华东区上个月可靠性试验的准时完成率是多少")
    south = e.ask("那华南区呢？")
    assert south["type"] == "result"
    # 华东=0.923，华南=0.887，多轮继承后区域被替换且结果不同
    assert east["rows"][0][0] != south["rows"][0][0]
    assert south["rows"][0][0] == 0.887


def test_ambiguity_clarification():
    e = _engine()
    e.reset_context()
    out = e.ask("那个做环境的实验室利用率怎么样")
    assert out["type"] == "clarification"
    assert "环境可靠性实验室" in out["message"]


def test_business_line_switch():
    e = _engine()
    out = e.ask("集成电路测试的检测一次通过率")
    assert out["type"] == "result"
    assert out["cols"] == ["first_pass_rate"]


def test_clarification_resume_keeps_metric():
    """确认澄清后应「回到原问题」：指标仍是用例里的设备利用率。

    回归用例：线上曾出现先问收入、再问"那个做环境的实验室利用率怎么样"、
    确认后却答成"检测服务收入"——因为澄清后把答复当成新问题，
    继承了上一轮的旧指标。
    """
    e = _engine()
    # 先制造一个"旧指标"（收入）留在上下文里，复现上述前置条件
    e.ask("华东区上个月可靠性试验的检测服务收入是多少")

    clar = e.ask("那个做环境的实验室利用率怎么样")
    assert clar["type"] == "clarification"

    out = e.ask("是的")
    assert out["type"] == "result"
    assert out["cols"] == ["utilization"], "指标应为设备利用率，而不是收入"
    assert out["mapped"].metric is not None
    assert out["mapped"].metric.id == "equipment_utilization"
    assert out["mapped"].entities.get("business_line") == "reliability"


def test_clarification_reply_with_extra_info():
    """带补充信息的确认（"是的，就是可靠性实验室"）也应回到原问题。"""
    e = _engine()
    e.ask("那个做环境的实验室利用率怎么样")
    out = e.ask("是的，就是可靠性实验室")
    assert out["type"] == "result"
    assert out["cols"] == ["utilization"]


def test_new_question_during_pending_is_not_folded():
    """待澄清期间用户直接抛出带指标的新问题 -> 应按新问题处理，不折叠。"""
    e = _engine()
    e.ask("那个做环境的实验室利用率怎么样")
    out = e.ask("集成电路测试的检测一次通过率")
    assert out["type"] == "result"
    assert out["cols"] == ["first_pass_rate"]
    assert e.pending is None


def test_reset_clears_pending():
    e = _engine()
    e.ask("那个做环境的实验室利用率怎么样")
    assert e.pending is not None
    e.reset_context()
    assert e.pending is None


# ---------------- 空结果回退：继承来的过滤条件把结果筛空时忽略它们重查 ----------------
# 真机 bug（用户报的）：先问「华东区上个月可靠性试验的准时完成率是多少」，
# 再问「EMC 检测的准时率是多少」——EMC 的报告全在北京（华北），
# 被继承来的「区域=华东」一过滤就为空，页面显示"无匹配数据"，用户以为系统算错了。

from tests.doubles import (  # noqa: E402
    GRGMockLLM,  # noqa: F401  与上面 _engine() 用的是同一批替身
)


class _GlossaryHonoringLLM(LLMClient):
    """模拟**真实 LLM 的行为**：照抄提示词里 glossary 的「必须使用 X = '值'」硬约束来拼 WHERE。

    这一点是必须模拟的：口径注入（Glossary）会把实体渲染成"必须使用的过滤条件"塞进提示词，
    真机上 LLM 会照做。第一版回退**没重建 Glossary**，于是重查时提示词里仍带着
    「必须使用 labs.region = '华东'」，SQL 又把区域写了回去，结果照样为空 ——
    回退静默失效。用一个"只看提示词、不看引擎内部状态"的替身才能把这类问题钉住。
    """

    def generate(self, prompt: str, system: str | None = None) -> str:
        filters = re.findall(r"必须使用 (\S+) = '([^']+)'", prompt)
        where = " AND ".join(f"{col} = '{val}'" for col, val in filters)
        return (
            "SELECT COUNT(*) FILTER (WHERE r.on_time = 1)::float / NULLIF(COUNT(*), 0) "
            "AS on_time_rate FROM reports r JOIN labs l ON r.lab_id = l.id "
            "JOIN business_lines b ON r.business_line_id = b.id "
            f"WHERE 1=1 {('AND ' + where) if where else ''}"
        )


class _EmcNorthOnlyDB(GRGSampleDB):
    """复现真机的数据分布：EMC 的报告只在华北；一旦带上「区域=华东」就查不到。"""

    def execute(self, sql: str):
        s = sql.upper()
        if "ON_TIME_RATE" in s and "'EMC'" in s:
            if "华东" in s:
                return (["on_time_rate"], [(None,)])     # 与真机一致：查询成功但取不到值
            return (["on_time_rate"], [(0.75,)])          # 全区域口径 = 0.75
        return super().execute(sql)


def _retry_engine() -> GRGQueryEngine:
    settings = Settings()
    registry = build_registry(settings.db.dialect)
    layer = build_semantic_layer()
    pipeline = Text2SQLPipeline(
        registry=registry, store=build_store(), llm=_GlossaryHonoringLLM(),
        db=_EmcNorthOnlyDB(registry, dialect=settings.db.dialect),
        graph=layer.graph,
        top_k=settings.retrieval.top_k, min_score=settings.retrieval.min_score,
        max_retry=settings.pipeline.max_retry,
    )
    return GRGQueryEngine(pipeline, layer)


def test_empty_result_relaxes_inherited_filters():
    """继承来的「区域=华东」把 EMC 筛空 -> 忽略继承维度重查，拿到 0.75 并如实告知。"""
    e = _retry_engine()
    first = e.ask("华东区上个月可靠性试验的准时完成率是多少")
    assert first["rows"][0][0] == 0.923          # GRGSampleDB：华东可靠性
    assert first.get("context_fallback") is None  # 有数据就不该回退

    second = e.ask("EMC 检测的准时率是多少")
    assert second["type"] == "result"
    assert second["rows"] == [(0.75,)], "应忽略继承的区域/时间后重查到全区域口径"
    assert second["context_fallback"]["dropped"] == ["region", "time"]
    assert second["mapped"].entities.get("region") is None
    assert any("空结果回退" in r for r in second["mapped"].reasons)


def test_explicit_filters_are_never_dropped_by_the_fallback():
    """用户**本轮明说**的过滤条件绝不能被擅自放宽（那就是改了用户的口径）。"""
    e = _retry_engine()
    out = e.ask("华东区 EMC 检测的准时率是多少")     # 华东是本轮显式说的

    assert out["mapped"].entities.get("region") == "华东"
    assert out.get("context_fallback") is None
    assert out["rows"] == [(None,)]                  # 保持"无匹配数据"，不假装有结果


def test_d03_customer_ranking_example_is_top_retrieval():
    """D03『已开票合同金额最高的客户是哪个』必须把客户排名示例排到第一，
    才能提供正确的 contracts→customers 连接示范与回退模板。"""
    from nl2sql.retrieval import build_retriever

    settings = Settings()
    store = build_store()
    retriever = build_retriever(settings, store)  # 无 embedder/dsn -> 纯标签检索
    hits = retriever.retrieve("已开票合同金额最高的客户是哪个", top_k=3)
    assert hits, "应检索到示例"
    assert hits[0].example.id == "ex_contract_amount_top_customer", (
        f"D03 应把客户排名示例排第一，实际: {[h.example.id for h in hits]}"
    )


def test_c05_retrieval_not_polluted_by_customer_example():
    """回归护栏：加 D03 客户排名示例后，C05『每个客户的合同总金额』的检索 top
    不能被它抢占（否则分组问句会误学 LIMIT 1 而退化）。示例标签刻意只挂
    『排名/最高/哪个』，不挂 合同金额/客户，正是为此。"""
    from nl2sql.retrieval import build_retriever

    settings = Settings()
    store = build_store()
    retriever = build_retriever(settings, store)
    hits = retriever.retrieve("每个客户的合同总金额是多少", top_k=3)
    top_ids = [h.example.id for h in hits]
    assert "ex_contract_amount_top_customer" not in top_ids, (
        f"C05 检索被客户排名示例污染: {top_ids}"
    )


# ---------------- 未识别槽位：不能静默沿用上一轮 ----------------
# 真机：先问「华南呢」-> 0.8667；再问「那华西 准确率呢」-> 还是 0.8667、SQL 里仍是华南。
# 根因：inherit() 只补"缺失"的槽位，把"用户提了但没懂"也当成"缺失"来补。

def test_unrecognized_region_clarifies_instead_of_reusing_context():
    e = _engine()
    first = e.ask("华南区上个月可靠性试验的准时完成率是多少")
    assert first["type"] == "result", "前置条件：第一轮应正常出结果并写进上下文"

    out = e.ask("那华西 准确率呢")

    assert out["type"] == "clarification", f"应澄清，实际 {out.get('type')}"
    assert "华西" in out["message"]
    assert "rows" not in out and "result" not in out, "澄清不该产生任何查询结果"


def test_clarification_converges_instead_of_looping():
    """澄清后给出有效值必须能出结果，不能反复澄清。"""
    e = _engine()
    e.ask("华南区上个月可靠性试验的准时完成率是多少")
    assert e.ask("那华西 准确率呢")["type"] == "clarification"

    out = e.ask("华南区的准时率是多少")

    assert out["type"] == "result", f"澄清后应能答上来，实际 {out.get('type')}"


def test_short_reply_narrows_only_the_remaining_slot():
    """回复"华南"只补上区域 -> 仍缺指标 -> 只就指标再问一次（渐进式，不是死循环）。"""
    e = _engine()
    e.ask("华南区上个月可靠性试验的准时完成率是多少")
    e.ask("那华西 准确率呢")

    again = e.ask("华南")
    assert again["type"] == "clarification"
    assert "准确率" in again["message"]
    assert "华西" not in again["message"], "区域已由回复解决，不该再问它"

    out = e.ask("准时率")
    assert out["type"] == "result", f"两个槽位补齐后应答上来，实际 {out.get('type')}"
