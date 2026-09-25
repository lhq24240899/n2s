"""计量检测引擎端到端测试：单轮、多轮、歧义澄清。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nl2sql.config import Settings
from nl2sql.pipeline import Text2SQLPipeline

from examples.grg_engine import GRGQueryEngine
from examples.grg_schema import build_registry, build_semantic_layer, build_store
from tests.doubles import GRGMockLLM, GRGSampleDB


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
