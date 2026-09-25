from nl2sql.linker import SchemaLinker
from nl2sql.retrieval import RetrievalService

from examples.schema import build_registry, build_store


def test_infer_from_question_table_name():
    linker = SchemaLinker(build_registry())
    names = linker.infer_tables("查一下 orders 表", hits=[])
    assert "orders" in names


def test_infer_from_hits():
    reg = build_registry()
    svc = RetrievalService(build_store())
    hits = svc.retrieve("昨天GMV是多少")
    linker = SchemaLinker(reg)
    names = linker.infer_tables("随便问问", hits)
    assert "orders" in names


def test_fallback():
    linker = SchemaLinker(build_registry(), fallback_tables=["orders"])
    names = linker.infer_tables("啥也没有", hits=[])
    assert names == ["orders"]
