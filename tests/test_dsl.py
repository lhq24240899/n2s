"""IR 与编译器的离线单测：QueryIR -> ES Query DSL / PPL，及响应解析。

不连真集群：全部是纯函数与构造数据，保证语法正确性由编译器兜底这件事被测试锁住。
"""
from __future__ import annotations

import pytest

from nl2sql.dsl import IRFilter, IRMetric, QueryIR, parse_es_response


# ---------------- IRFilter / IRMetric ----------------

def test_filter_to_es_term_and_range():
    assert IRFilter("level", "term", "ERROR").to_es() == {"term": {"level": "ERROR"}}
    assert IRFilter("ts", "gte", "now-7d").to_es() == {"range": {"ts": {"gte": "now-7d"}}}
    assert IRFilter("region", "terms", ["华东", "华南"]).to_es() == {
        "terms": {"region": ["华东", "华南"]}
    }


def test_filter_to_ppl():
    assert IRFilter("level", "term", "ERROR").to_ppl() == "level = 'ERROR'"
    assert IRFilter("region", "terms", ["华东", "华南"]).to_ppl() == "region in ('华东', '华南')"
    assert IRFilter("ts", "gte", "now-7d").to_ppl() == "ts >= now-7d"


def test_metric_count_emits_empty_es_but_named_ppl():
    assert IRMetric("cnt", "count").to_es() == {}
    assert IRMetric("cnt", "count").to_ppl() == "count() as cnt"
    assert IRMetric("avg_ts", "avg", "v").to_es() == {"avg": {"field": "v"}}
    assert IRMetric("avg_ts", "avg", "v").to_ppl() == "avg(v) as avg_ts"


# ---------------- IR.validate ----------------

def _ir(**kw) -> QueryIR:
    base = dict(index="device_events", metrics=[IRMetric("cnt", "count")])
    base.update(kw)
    return QueryIR(**base)


def test_validate_happy_path():
    assert _ir().validate() is None


def test_validate_rejects_bad_shapes():
    assert "index" in QueryIR(index="", metrics=[IRMetric("cnt", "count")]).validate()
    assert "聚合" in _ir(metrics=[]).validate()
    assert "非法过滤" in _ir(filters=[IRFilter("x", "bogus", 1)]).validate()
    assert "需要字段" in _ir(metrics=[IRMetric("a", "avg")]).validate()
    assert "非法排序" in _ir(order_dir="sideways").validate()


# ---------------- to_es_dsl ----------------

def test_es_dsl_global_count_uses_hits_total():
    body = _ir().to_es_dsl()
    assert body["size"] == 0
    assert body["query"] == {"match_all": {}}
    # 全局 count 不该产生聚合（count 由 hits.total.value 表达）
    assert body.get("aggs", {}).get("cnt") is None


def test_es_dsl_global_avg_metric_present():
    body = _ir(metrics=[IRMetric("avg_v", "avg", "v")]).to_es_dsl()
    assert body["aggs"] == {"avg_v": {"avg": {"field": "v"}}}


def test_es_dsl_filters_go_under_bool_filter():
    body = _ir(filters=[IRFilter("level", "term", "ERROR")]).to_es_dsl()
    assert body["query"] == {"bool": {"filter": [{"term": {"level": "ERROR"}}]}}


def test_es_dsl_group_count_orders_by_internal_count():
    body = _ir(group_by="region", order_by="cnt", order_dir="desc").to_es_dsl()
    terms = body["aggs"]["group"]["terms"]
    assert terms["field"] == "region"
    # count 指标在分组里是 doc_count，排序键必须是 ES 内置 _count（默认 key 会找不到 cnt）
    assert terms["order"] == {"_count": "desc"}


def test_es_dsl_group_asc_for_least():
    body = _ir(group_by="region", order_by="cnt", order_dir="asc", limit=1).to_es_dsl()
    terms = body["aggs"]["group"]["terms"]
    assert terms["order"] == {"_count": "asc"}
    assert terms["size"] == 1


def test_es_dsl_group_with_non_count_metric_orders_by_metric_name():
    body = _ir(
        group_by="region",
        metrics=[IRMetric("avg_v", "avg", "v")],
        order_by="avg_v",
    ).to_es_dsl()
    assert body["aggs"]["group"]["terms"]["order"] == {"avg_v": "desc"}
    assert body["aggs"]["group"]["aggs"] == {"avg_v": {"avg": {"field": "v"}}}


# ---------------- to_ppl ----------------

def test_ppl_global_count():
    ppl = _ir().to_ppl()
    assert ppl == "source=device_events | stats count() as cnt | head 20"


def test_ppl_group_filter_sort():
    ir = _ir(
        filters=[IRFilter("level", "term", "ERROR"), IRFilter("ts", "gte", "now-7d")],
        group_by="region",
        order_by="cnt",
        order_dir="desc",
        limit=3,
    )
    ppl = ir.to_ppl()
    assert ppl == (
        "source=device_events | where level = 'ERROR' and ts >= now-7d "
        "| stats count() as cnt by region | sort - cnt | head 3"
    )


# ---------------- parse_es_response ----------------

def test_parse_group_response():
    ir = _ir(group_by="region")
    resp = {"aggregations": {"group": {"buckets": [
        {"key": "华东", "doc_count": 42},
        {"key": "华南", "doc_count": 30},
    ]}}}
    cols, rows = parse_es_response(resp, ir)
    assert cols == ["region", "cnt"]
    assert rows == [("华东", 42), ("华南", 30)]


def test_parse_global_count_uses_hits_total():
    ir = _ir()
    resp = {"hits": {"total": {"value": 1222}}}
    cols, rows = parse_es_response(resp, ir)
    assert cols == ["cnt"]
    assert rows == [(1222,)]


def test_parse_global_avg_uses_aggregation():
    ir = _ir(metrics=[IRMetric("avg_v", "avg", "v")])
    resp = {"aggregations": {"avg_v": {"value": 3.5}}}
    cols, rows = parse_es_response(resp, ir)
    assert rows == [(3.5,)]
