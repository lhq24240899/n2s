"""EsQueryEngine / HybridRouter 的离线测试。

用 httpx.MockTransport 模拟 ES 响应 -> 走完整的「问题->IR->DSL->HTTP->解析」链路，
既验证确定性填槽规则，又验证执行器在真实 HTTP 边界上的行为（鉴权头/只读/错误回落）。
"""
from __future__ import annotations

import json

import httpx
import pytest

from nl2sql.dsl import IRFilter, QueryIR
from nl2sql.es_backend import ElasticsearchBackend, EsError
from examples.es_engine import EsQueryEngine, HybridRouter


# ---------------- MockTransport 工厂 ----------------

def _transport_handler(responses: list[dict]):
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        path = request.url.raw_path.decode()
        calls.append({"path": path, "body": body})
        if path == "/_plugins/_ppl":
            # OpenSearch PPL 响应契约：schema + datarows
            # ⚠️ 列序与真实集群一致：`stats count() as cnt by region` 返回的是
            #    ['cnt', 'region']（聚合列在 by 字段**之前**），而不是直觉上的 [分组, 指标]。
            #    早期这个 mock 写成了理想顺序，把真实的列序 bug 掩盖了（真机跑到 0/9 才暴露）。
            payload = {
                "schema": [{"name": "cnt", "type": "long"}, {"name": "region", "type": "string"}],
                "datarows": [[42, "华东"], [30, "华南"]],
                "status": 200,
            }
        elif "group" in body.get("aggs", {}):
            buckets = [
                {"key": "华东", "doc_count": 42},
                {"key": "华南", "doc_count": 30},
            ]
            payload = {"aggregations": {"group": {"buckets": buckets}}}
        else:
            payload = {"hits": {"total": {"value": 97}}}
        return httpx.Response(200, json=payload)

    return handler, calls


def _engine():
    handler, calls = _transport_handler([])
    backend = ElasticsearchBackend(
        host="https://es.example:9200",
        user="elastic",
        password="x",
        transport=httpx.MockTransport(handler),
    )
    return EsQueryEngine(backend, index="device_events"), calls


# ---------------- 域识别 ----------------

def test_is_es_domain():
    eng, _ = _engine()
    assert eng.is_es_domain("最近7天有多少告警")
    assert not eng.is_es_domain("华南区的收入是多少")


# ---------------- build_ir：确定性填槽 ----------------

def test_build_ir_level_region_time():
    eng, _ = _engine()
    ir = eng.build_ir("华东区最近7天的 ERROR 告警有多少")
    fields = {f.field: (f.op, f.value) for f in ir.filters}
    assert fields["level"] == ("term", "ERROR")
    assert fields["region"] == ("term", "华东")
    assert fields["ts"] == ("gte", "now-7d")
    assert ir.group_by == "level"          # 「多少」无显式分组 -> level 分布
    assert ir.metrics[0].agg == "count"


def test_build_ir_group_by_region_and_topn():
    eng, _ = _engine()
    ir = eng.build_ir("告警最多的区域是哪个")
    assert ir.group_by == "region"
    assert ir.order_by == "cnt" and ir.order_dir == "desc" and ir.limit == 1


def test_build_ir_least_sets_asc():
    eng, _ = _engine()
    ir = eng.build_ir("告警最少的实验室")
    assert ir.group_by == "lab_name"
    assert ir.order_dir == "asc" and ir.limit == 1


def test_build_ir_message_match():
    eng, _ = _engine()
    ir = eng.build_ir("包含「温度超限」的告警")
    assert any(f.field == "message" and f.op == "match" and f.value == "温度超限"
               for f in ir.filters)


def test_scope_filters_prepended_and_preserved():
    eng, _ = _engine()
    scope = [IRFilter("region", "terms", ["华东"])]
    ir = eng.build_ir("最近7天的告警", scope_filters=scope)
    # 权限过滤必须保留在最前
    assert ir.filters[0] == IRFilter("region", "terms", ["华东"])


# ---------------- ask：完整链路（含 HTTP） ----------------

def test_ask_group_end_to_end():
    eng, calls = _engine()
    out = eng.ask("各区域的告警数量")
    assert out["type"] == "result" and out["engine"] == "es"
    assert out["columns"] == ["region", "cnt"]
    assert ("华东", 42) in out["rows"]
    # 必须同时给出两种编译产物；mock 端点响应 200 -> PPL 状态为已执行
    assert "source=device_events" in out["ppl"]["query"]
    assert out["ppl"]["status"] == "executed"
    assert out["ppl"]["rows"] == [("华东", 42), ("华南", 30)]
    # 确认请求打到了正确的 _search 路径且 size=0
    assert calls[0]["path"] == "/device_events/_search"
    assert calls[0]["body"]["size"] == 0


def test_ppl_columns_reordered_to_dsl_contract():
    """真机踩坑：OpenSearch 的 PPL 把聚合列放在 by 字段**之前**（schema=['cnt','region']）。

    必须按 ir.group_by 重排成 ['region','cnt'] 对齐 DSL 侧契约，
    否则上层按 [key, value] 取值会整体错位（表现为"区域名和数字互换"）。
    """
    handler, _ = _transport_handler([])
    backend = ElasticsearchBackend(
        host="https://es.example:9200", user="elastic", password="x",
        transport=httpx.MockTransport(handler),
    )
    eng, _ = _engine()
    ir = eng.build_ir("各区域的告警数量")
    cols, rows = backend.execute_ppl(
        "source=device_events | stats count() as cnt by region", ir=ir)
    assert cols == ["region", "cnt"]
    assert rows == [("华东", 42), ("华南", 30)]


def test_ppl_cjk_literal_raises_actionable_error():
    """Calcite 按 ISO-8859-1 编码字面量：PPL 里任何中文字面量都必然 500。

    这是 OpenSearch PPL 的引擎限制（换 U&'..' / like / match / 双引号都绕不过），
    所以要给出**可操作的提示**，而不是把原始堆栈丢给用户。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {
            "reason": "There was internal problem at backend",
            "details": "Failed to encode '华东' in character set 'ISO-8859-1'",
            "type": "CalciteException"}})

    backend = ElasticsearchBackend(
        host="https://os.example:26380", user="avnadmin", password="x",
        transport=httpx.MockTransport(handler),
    )
    eng, _ = _engine()
    ir = eng.build_ir("华东区最近7天的ERROR告警数量")
    with pytest.raises(EsError) as ei:
        backend.execute_ppl(
            "source=device_events | where region = '华东' | stats count() as cnt", ir=ir)
    msg = str(ei.value)
    assert "非 ASCII" in msg and "DSL" in msg


def test_headers_do_not_pin_es_major_version():
    """真机踩坑：compatible-with=8/9 在 ES 9.3 上是非法 media type（400）。

    客户端不该绑架集群版本——用通用 application/json，7/8/9 通吃。
    """
    backend = ElasticsearchBackend(
        "https://es.example:9200",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    accept = backend._headers()["Accept"]
    assert "compatible-with" not in accept
    assert accept == "application/json"


def test_ask_backend_error_surfaces():
    def boom(request):
        return httpx.Response(500, json={"error": "boom"})

    backend = ElasticsearchBackend(
        "https://es.example:9200", transport=httpx.MockTransport(boom))
    eng = EsQueryEngine(backend)
    out = eng.ask("最近7天告警")
    assert out["type"] == "error"


# ---------------- HybridRouter：域路由 + 失败回落 ----------------

class _FakeSql:
    def __init__(self):
        self.asked = []

    def ask(self, question):
        self.asked.append(question)
        return {"type": "result", "engine": "sql", "question": question}


def test_router_sends_event_question_to_es():
    sql = _FakeSql()
    eng, _ = _engine()
    router = HybridRouter(sql, eng)
    out = router.ask("最近7天有多少告警")
    assert out["engine"] == "es"
    assert sql.asked == []


def test_router_sends_metric_question_to_sql():
    sql = _FakeSql()
    eng, _ = _engine()
    router = HybridRouter(sql, eng)
    router.ask("华南区收入是多少")
    assert sql.asked == ["华南区收入是多少"]


def test_router_falls_back_when_es_errors():
    sql = _FakeSql()

    def boom(request):
        return httpx.Response(500, json={"error": "x"})

    backend = ElasticsearchBackend(
        "https://es.example:9200", transport=httpx.MockTransport(boom))
    eng = EsQueryEngine(backend)
    router = HybridRouter(sql, eng)
    out = router.ask("最近7天告警")
    assert out["engine"] == "sql"
    assert any("回落" in r for r in out.get("reasons", []))


def test_router_without_es_goes_all_sql():
    sql = _FakeSql()
    router = HybridRouter(sql, None)
    router.ask("最近7天告警")
    assert sql.asked == ["最近7天告警"]


# ---------------- 多轮上下文（与 SQL 路径的 QueryContext 同构） ----------------
# 规则：本轮显式说了的维度覆盖上一轮；没说、上轮说过的补齐；本轮要分组的维度不作过滤条件继承。

def _filters(out: dict) -> dict:
    return {f: (op, v) for f, op, v in out["entities"]["filters"]}


def test_follow_up_marker_classification():
    from examples.es_engine import is_follow_up

    assert is_follow_up("那华南区呢？")
    assert is_follow_up("那最近30天呢")
    assert is_follow_up("华南区")
    assert is_follow_up("换成最近30天")
    assert is_follow_up("还有华北吗")
    # 全新问题不应被当成追问
    assert not is_follow_up("各区域最近7天的ERROR告警数量")
    assert not is_follow_up("最近7天各级别有多少条日志")
    assert not is_follow_up("告警最多的区域是哪个")
    assert not is_follow_up("")


def test_multi_turn_replaces_only_the_named_dimension():
    """「那华南区呢」= 只换区域，级别与时间窗口沿用上一轮。"""
    eng, _ = _engine()
    first = eng.ask("华东区最近7天的ERROR告警有多少条")
    assert _filters(first)["region"] == ("term", "华东")

    second = eng.ask("那华南区呢？")
    got = _filters(second)
    assert got["region"] == ("term", "华南")       # 替换
    assert got["level"] == ("term", "ERROR")       # 继承
    assert got["ts"] == ("gte", "now-7d")          # 继承
    assert any("维度替换" in r for r in second["reasons"])


def test_follow_up_chain_keeps_both_dimensions():
    """链式追问：华东 → 那华南呢 → 那最近30天呢，最终应是 华南 + 30天。"""
    eng, _ = _engine()
    eng.ask("华东区最近7天的ERROR告警有多少条")
    eng.ask("那华南区呢？")
    third = eng.ask("那最近30天呢？")
    got = _filters(third)
    assert got["region"] == ("term", "华南")
    assert got["ts"] == ("gte", "now-30d")
    assert got["level"] == ("term", "ERROR")


def test_grouping_dimension_is_not_inherited_as_filter():
    """本轮要按区域分组时，不得继承上一轮的 region 过滤（否则只剩一行）。"""
    eng, _ = _engine()
    eng.ask("华东区最近7天的ERROR告警有多少条")
    out = eng.ask("各区域最近7天的ERROR告警数量")
    assert "region" not in _filters(out)


def test_new_question_does_not_inherit_previous_dimensions():
    """全新问题不继承，避免"点几个示例问题就串味"。"""
    eng, _ = _engine()
    eng.ask("华东区最近7天的ERROR告警有多少条")
    out = eng.ask("最近7天各级别有多少条日志")
    got = _filters(out)
    assert "region" not in got and "level" not in got
    assert any("不继承" in r for r in out["reasons"])


def test_reset_context_clears_inheritance():
    eng, _ = _engine()
    eng.ask("华东区最近7天的ERROR告警有多少条")
    eng.reset_context()
    out = eng.ask("那华南区呢？")
    # 上下文已清空：追问只带上本轮显式提到的区域
    assert _filters(out) == {"region": ("term", "华南")}


def test_scope_filters_survive_multi_turn_and_are_not_replaced():
    """行级权限是每轮重新施加的安全约束：不得被多轮继承"覆盖掉"。

    当权限过滤与用户问题过滤**同一字段**时，二者会同时在 DSL 里（AND 语义）→
    受限于华东的用户问华南会拿到空结果，但**不会越权**（fail-closed）。
    这不是 bug：宁可返回空，也不能把权限条件悄悄改写或丢弃。
    """
    eng, _ = _engine()
    scope = [IRFilter("region", "term", "华东")]
    eng.ask("华东区最近7天的ERROR告警有多少条", scope_filters=scope)
    out = eng.ask("那华南区呢？", scope_filters=scope)

    pairs = [(f, op, v) for f, op, v in out["entities"]["filters"]]
    assert ("region", "term", "华东") in pairs      # 权限过滤仍在（每轮由调用方传入）
    assert ("region", "term", "华南") in pairs      # 用户本轮显式说的维度也在
    # 两个 term 同时存在于 DSL -> 交集为空，是预期的 fail-closed 行为
    assert out["es_dsl"]["query"]["bool"]["filter"].count({"term": {"region": "华东"}}) == 1
