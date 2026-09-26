"""执行引擎「按模式装配」的离线测试（ES DSL / OpenSearch PPL 双端点）。

为什么单独测这一层？
  网页上的 SQL / DSL / PPL 三档切换，底层就是 `build_es_backend(settings, mode)` 指向不同端点。
  一旦这里的回退逻辑错了（比如 PPL 模式误用 ES 端点、或把未配置当成已配置），
  表现是"页面能点但查的不是那台集群" —— 这种问题在真机上很难一眼看出来，必须在配置层守住。

覆盖点：
  1. 未启用 → 返回 None（上层优雅降级，不抛异常）
  2. PPL 模式优先用 ES__PPL_HOST；留空则回退 ES__HOST
  3. 只配了 OpenSearch（无 ES）时，PPL 仍可用；但 ES 模式应为 None
  4. es 模式绝不使用 PPL 端点（避免"切了引擎却仍查 ES"）
  5. resolve_index 的索引名解析与回退
"""
from __future__ import annotations

from types import SimpleNamespace

from nl2sql.es_backend import build_es_backend, resolve_index


def _settings(**es_kwargs) -> SimpleNamespace:
    """构造最小 settings 替身：只需要 .es 这一段。"""
    base = dict(
        enabled=False, host="", user="elastic", password="", index="device_events",
        timeout=15.0,
        ppl_enabled=False, ppl_host="", ppl_user="", ppl_password="",
        ppl_index="", ppl_timeout=30.0,
    )
    base.update(es_kwargs)
    return SimpleNamespace(es=SimpleNamespace(**base))


# ---------------- 1) 未启用 ----------------

def test_disabled_returns_none_for_both_modes():
    s = _settings(enabled=False, host="http://es:9200")
    assert build_es_backend(s, mode="es") is None
    assert build_es_backend(s, mode="ppl") is None


def test_no_settings_object_returns_none():
    assert build_es_backend(SimpleNamespace(), mode="es") is None
    assert build_es_backend(SimpleNamespace(), mode="ppl") is None


# ---------------- 2) 端点选择与回退 ----------------

def test_ppl_mode_prefers_dedicated_ppl_host():
    s = _settings(enabled=True, host="http://es-aliyun:9200",
                  ppl_enabled=True, ppl_host="https://aiven-opensearch:26380")
    b = build_es_backend(s, mode="ppl")
    assert b is not None and b.host == "https://aiven-opensearch:26380"


def test_ppl_mode_falls_back_to_es_host_when_no_ppl_host():
    """未配 PPL 端点时复用 ES 端点：此时普通 ES 无 _plugins/_ppl，
    由 EsQueryEngine 把 PPL 降级成「仅编译」，而不是在这里直接失败。"""
    s = _settings(enabled=True, host="http://es-only:9200")
    b = build_es_backend(s, mode="ppl")
    assert b is not None and b.host == "http://es-only:9200"


def test_ppl_only_config_works_without_es_enabled():
    """只开了 OpenSearch（不配 Elasticsearch）也要能用 PPL。"""
    s = _settings(enabled=False, host="", ppl_enabled=True,
                  ppl_host="https://aiven:26380", ppl_user="avnadmin", ppl_password="pw")
    assert build_es_backend(s, mode="es") is None
    b = build_es_backend(s, mode="ppl")
    assert b is not None and b.host == "https://aiven:26380"


def test_es_mode_never_uses_ppl_endpoint():
    """切到 DSL 档时必须打 Elasticsearch，不能因为配了 PPL 端点就改道。"""
    s = _settings(enabled=True, host="http://es-aliyun:9200",
                  ppl_enabled=True, ppl_host="https://aiven:26380")
    b = build_es_backend(s, mode="es")
    assert b is not None and b.host == "http://es-aliyun:9200"


def test_ppl_credentials_fall_back_to_es_credentials(monkeypatch):
    """PPL 端点没单独配账号时，应复用 ES__USER / ES__PASSWORD。"""
    captured: dict = {}

    class _Spy:
        def __init__(self, host, user="", password="", timeout=15.0, transport=None):
            captured.update(host=host, user=user, password=password, timeout=timeout)

    monkeypatch.setattr("nl2sql.es_backend.ElasticsearchBackend", _Spy)

    s = _settings(enabled=True, host="http://es:9200", user="elastic", password="es-pw",
                  ppl_enabled=True, ppl_host="https://os:26380")
    build_es_backend(s, mode="ppl")
    assert captured["host"] == "https://os:26380"
    assert captured["user"] == "elastic" and captured["password"] == "es-pw"
    assert captured["timeout"] == 30.0          # PPL 用独立超时（首查较慢）


def test_ppl_dedicated_credentials_win(monkeypatch):
    captured: dict = {}

    class _Spy:
        def __init__(self, host, user="", password="", timeout=15.0, transport=None):
            captured.update(user=user, password=password)

    monkeypatch.setattr("nl2sql.es_backend.ElasticsearchBackend", _Spy)

    s = _settings(enabled=True, host="http://es:9200", user="elastic", password="es-pw",
                  ppl_enabled=True, ppl_host="https://os:26380",
                  ppl_user="avnadmin", ppl_password="os-pw")
    build_es_backend(s, mode="ppl")
    assert captured["user"] == "avnadmin" and captured["password"] == "os-pw"


# ---------------- 3) 索引名解析 ----------------

def test_resolve_index_defaults_and_overrides():
    s = _settings(index="device_events")
    assert resolve_index(s, "es") == "device_events"
    assert resolve_index(s, "ppl") == "device_events"      # 未单独指定 -> 复用

    s2 = _settings(index="device_events", ppl_index="device_events_os")
    assert resolve_index(s2, "es") == "device_events"
    assert resolve_index(s2, "ppl") == "device_events_os"


def test_resolve_index_handles_empty_config():
    assert resolve_index(SimpleNamespace(), "es") == "device_events"
    assert resolve_index(_settings(index=""), "es") == "device_events"
