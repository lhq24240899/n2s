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


# ---------------- 业务知识图谱 -> Schema Linking ----------------

def test_graph_maps_chinese_terms_to_tables():
    """中文业务词（实验室/设备）必须能通过知识图谱落到物理表上。"""
    from examples.grg_schema import build_registry as grg_registry
    from examples.grg_schema import build_semantic_layer

    layer = build_semantic_layer()
    linker = SchemaLinker(grg_registry(), graph=layer.graph)
    names = linker.infer_tables("各实验室的设备利用率是多少", hits=[])
    assert "labs" in names and "equipment" in names
    assert any(r.startswith("知识图谱") for r in linker.last_reasons)


def test_graph_off_keeps_old_behavior():
    """不传图谱时行为与旧版一致（该问题抽不到表）。"""
    from examples.grg_schema import build_registry as grg_registry

    linker = SchemaLinker(grg_registry())
    names = linker.infer_tables("各实验室的设备利用率是多少", hits=[])
    assert "equipment" not in names


def test_graph_only_uses_table_edges():
    """非「对应表」关系（如 实验室-属于-区域）不能把概念词当表名塞进候选。"""
    from examples.grg_schema import build_registry as grg_registry
    from examples.grg_schema import build_semantic_layer

    layer = build_semantic_layer()
    linker = SchemaLinker(grg_registry(), graph=layer.graph)
    names = linker.infer_tables("报告有哪些", hits=[])
    assert set(names) in ({"reports"}, {"reports", "labs"})
    assert "大区" not in names and "区域" not in names
