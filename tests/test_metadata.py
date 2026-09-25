"""元数据接入层的离线单测：指纹、diff、API 解析与缓存。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nl2sql.metadata import (
    ApiMetadataProvider,
    StaticMetadataProvider,
    build_metadata_provider,
    diff_tables,
    fingerprint,
    format_diff,
)
from nl2sql.models import TableSchema


def _tables() -> list[TableSchema]:
    return [
        TableSchema(
            name="reports",
            columns={"id": "int", "on_time": "int", "amount": "decimal"},
            description="报告表",
            foreign_keys=[("lab_id", "labs", "id")],
            sample_values={"status": ["已出具"]},
        ),
        TableSchema(name="labs", columns={"id": "int", "region": "varchar"}),
    ]


# ---------------- Static 提供者与指纹 ----------------

def test_static_provider_loads():
    assert [t.name for t in StaticMetadataProvider(_tables).load()] == ["reports", "labs"]


def test_fingerprint_stable_and_sensitive():
    assert fingerprint(_tables()) == fingerprint(_tables())          # 稳定
    changed = [TableSchema(name="reports", columns={"id": "int", "on_time": "int", "amount": "numeric"})]
    assert fingerprint(_tables()) != fingerprint(changed)            # 类型变了要能发现
    added = _tables() + [TableSchema(name="samples", columns={"id": "int"})]
    assert fingerprint(_tables()) != fingerprint(added)              # 加表要能发现


# ---------------- diff ----------------

def test_diff_tables_reports_add_remove_and_type_change():
    old = _tables()
    new = [
        TableSchema(name="reports", columns={"id": "int", "on_time": "int", "amount": "numeric", "note": "varchar"}),
        TableSchema(name="labs", columns={"id": "int", "region": "varchar"}),
        TableSchema(name="samples", columns={"id": "int"}),
    ]
    d = diff_tables(old, new)
    assert d["added_tables"] == ["samples"]
    assert d["removed_tables"] == []
    ch = d["changed_columns"]["reports"]
    assert ch["added"] == ["note"]
    assert ch["removed"] == []
    assert ch["type_changed"] == [("amount", "decimal", "numeric")]


def test_format_diff_renders_human_readable():
    d = {"added_tables": ["samples"], "removed_tables": ["legacy"], "changed_columns": {
        "reports": {"added": ["note"], "removed": [], "type_changed": [("amount", "decimal", "numeric")]}}}
    text = format_diff(d)
    assert "samples" in text and "legacy" in text and "decimal -> numeric" in text
    assert format_diff({"added_tables": [], "removed_tables": [], "changed_columns": {}}) == "无结构变更"


# ---------------- Api 提供者（注入 fetch，不联网） ----------------

_API_DATA = [
    {
        "name": "reports",
        "description": "报告表",
        "columns": {"id": "int", "on_time": "int"},
        "foreign_keys": [["lab_id", "labs", "id"]],
        "sample_values": {"status": ["已出具"]},
    },
    {"name": "labs", "columns": {"id": "int", "region": "varchar"}},
]


def test_api_provider_parses_and_caches():
    calls = {"n": 0}

    def fake_fetch(url, headers):
        calls["n"] += 1
        assert url.endswith("/tables")
        return _API_DATA

    p = ApiMetadataProvider("http://meta.internal", token="t", cache_ttl=60, fetch_fn=fake_fetch)
    tables = p.load()
    assert [t.name for t in tables] == ["reports", "labs"]
    assert tables[0].foreign_keys == [("lab_id", "labs", "id")]
    p.load()                      # TTL 内命中缓存
    assert calls["n"] == 1


def test_api_provider_rejects_empty_and_bad_payload():
    p = ApiMetadataProvider("http://meta.internal", fetch_fn=lambda url, h: [])
    with pytest.raises(ValueError):
        p.load()
    p2 = ApiMetadataProvider("http://meta.internal", fetch_fn=lambda url, h: [{"name": "x"}])  # 缺 columns -> 默认空
    assert p2.load()[0].name == "x"


def test_api_provider_requires_base_url():
    with pytest.raises(ValueError):
        ApiMetadataProvider("")


def test_build_provider_defaults_to_static():
    class _S:  # 最小 settings 替身
        class metadata:  # noqa: N801
            provider = "static"
            base_url = ""
            token = ""
            timeout = 5.0
            cache_ttl = 60.0

    from examples.grg_schema import build_tables

    provider = build_metadata_provider(_S())
    assert isinstance(provider, StaticMetadataProvider)
    assert {t.name for t in provider.load()} >= {"reports", "labs"}   # 与领域知识库一致
