"""智能体 API 端到端测试（离线）：认证 / 授权 / 数据权限 / 限流 / 会话隔离 / 审计。

用的是真实的 QueryService + 引擎 + 语义层 + 权限层，只有 LLM 与 DB 是确定性替身。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nl2sql.api import create_app
from nl2sql.auth import Authenticator, Role
from nl2sql.policy import DataPolicy

from tests.service_factory import SECRET, make_service, make_settings


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture()
def env():
    settings = make_settings()
    service = make_service(settings)
    app = create_app(service=service, settings=settings)
    auth = Authenticator(settings.auth)
    with TestClient(app) as client:
        yield {
            "client": client,
            "service": service,
            "settings": settings,
            "auth": auth,
            "token": lambda sub="alice", role=Role.ANALYST, **kw: auth.issue(sub, role, **kw),
        }


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------

def test_health_without_auth(env):
    r = env["client"].get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "sessions" in body and "concurrency" in body


def test_ask_without_token_is_401(env):
    r = env["client"].post("/v1/ask", json={"question": "华东区准时率"})
    assert r.status_code == 401
    assert "Bearer" in r.headers.get("www-authenticate", "")


def test_invalid_token_is_401(env):
    r = env["client"].post(
        "/v1/ask", json={"question": "华东区准时率"}, headers=_h("not-a-jwt")
    )
    assert r.status_code == 401


def test_expired_token_is_401(env):
    r = env["client"].get("/v1/whoami", headers=_h(env["token"](ttl_seconds=-5)))
    assert r.status_code == 401


def test_whoami_lists_scopes(env):
    r = env["client"].get("/v1/whoami", headers=_h(env["token"]()))
    assert r.status_code == 200
    assert r.json()["role"] == "analyst"
    assert "query:ask" in r.json()["scopes"]


def test_dev_token_endpoint_issues_usable_token(env):
    r = env["client"].post("/v1/auth/token", json={"sub": "bob", "role": "analyst"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    assert env["client"].get("/v1/whoami", headers=_h(token)).json()["sub"] == "bob"


def test_dev_token_endpoint_can_be_disabled():
    settings = make_settings(dev_token=False)
    app = create_app(service=make_service(settings), settings=settings)
    with TestClient(app) as client:
        assert client.post("/v1/auth/token", json={"sub": "x"}).status_code == 404


# ---------------------------------------------------------------------------
# 授权（角色 -> scope）
# ---------------------------------------------------------------------------

def test_viewer_cannot_ask(env):
    r = env["client"].post(
        "/v1/ask", json={"question": "华东区准时率"},
        headers=_h(env["token"](role=Role.VIEWER)),
    )
    assert r.status_code == 403
    assert "query:ask" in r.json()["detail"]


def test_viewer_can_read_schema_but_not_audit(env):
    token = env["token"](role=Role.VIEWER)
    assert env["client"].get("/v1/schema", headers=_h(token)).status_code == 200
    assert env["client"].get("/v1/audit", headers=_h(token)).status_code == 403


def test_audit_requires_admin(env):
    assert env["client"].get("/v1/audit", headers=_h(env["token"]())).status_code == 403
    token = env["token"]("ops", Role.ADMIN)
    assert env["client"].get("/v1/audit", headers=_h(token)).status_code == 200


# ---------------------------------------------------------------------------
# 问数主流程
# ---------------------------------------------------------------------------

def test_analyst_ask_result(env):
    r = env["client"].post(
        "/v1/ask",
        json={"question": "华东区上个月可靠性试验的准时完成率是多少"},
        headers=_h(env["token"]()),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "result"
    assert body["row_count"] == 1
    assert body["sql"] and body["columns"] == ["on_time_rate"]
    assert body["metric"] == "检测准时率"  # 指标口径名（来自语义层，不是用户原话）
    assert body["request_id"] and body["latency_ms"] >= 0


def test_kb_search_without_kb_returns_empty(env):
    r = env["client"].post(
        "/v1/kb/search", json={"query": "EMC 是什么"}, headers=_h(env["token"]())
    )
    assert r.status_code == 200
    assert r.json()["hits"] == []
    assert "知识库未启用" in r.json()["note"]


def test_schema_catalog_contains_metrics_and_scope(env):
    r = env["client"].get("/v1/schema", headers=_h(env["token"]()))
    body = r.json()
    assert body["role"] == "analyst"
    assert any(m["id"] == "on_time_completion_rate" for m in body["metrics"])
    assert any(t["name"] == "reports" for t in body["tables"])
    assert body["data_scope"]  # 至少包含"敏感字段不下发"这类说明


# ---------------------------------------------------------------------------
# 会话：多轮继承 + 用户隔离 + 重置
# ---------------------------------------------------------------------------

def test_multi_turn_inside_same_session(env):
    client, token = env["client"], env["token"]()
    first = client.post(
        "/v1/ask",
        json={"question": "华东区上个月可靠性试验的准时完成率是多少", "session_id": "s-1"},
        headers=_h(token),
    ).json()
    second = client.post(
        "/v1/ask", json={"question": "那华南区呢？", "session_id": "s-1"}, headers=_h(token)
    ).json()
    assert first["rows"][0][0] == 0.923          # 华东（替身固定值）
    assert second["rows"][0][0] == 0.887         # 华南：区域被替换、指标/业务线继承
    assert any("上下文继承" in r for r in second["reasons"])


def test_session_cannot_be_hijacked_by_another_user(env):
    client = env["client"]
    client.post(
        "/v1/ask",
        json={"question": "华东区准时率是多少", "session_id": "shared"},
        headers=_h(env["token"]("alice")),
    )
    r = client.post(
        "/v1/ask", json={"question": "那华南区呢？", "session_id": "shared"},
        headers=_h(env["token"]("bob")),
    )
    assert r.status_code == 403
    assert "属于其它用户" in r.json()["error"]["message"]


def test_reset_session(env):
    client, token = env["client"], env["token"]()
    client.post("/v1/ask", json={"question": "华东区准时率", "session_id": "s-2"}, headers=_h(token))
    r = client.post("/v1/sessions/s-2/reset", headers=_h(token))
    assert r.status_code == 200 and r.json()["reset"] is True


# ---------------------------------------------------------------------------
# 限流 / 并发
# ---------------------------------------------------------------------------

def test_rate_limit_returns_429_with_retry_after():
    settings = make_settings(rate_limit=1)   # 容量 1：第二次立即被限
    app = create_app(service=make_service(settings), settings=settings)
    auth = Authenticator(settings.auth)
    token = auth.issue("alice", Role.ANALYST)
    with TestClient(app) as client:
        first = client.post("/v1/ask", json={"question": "华东区准时率"}, headers=_h(token))
        second = client.post("/v1/ask", json={"question": "华东区准时率"}, headers=_h(token))
    assert first.status_code == 200
    assert second.status_code == 429
    assert "Retry-After" in second.headers
    assert second.json()["error"]["type"] == "RateLimited"


# ---------------------------------------------------------------------------
# 数据权限（行级过滤 / 指标拒绝）——通过 API 全链路验证
# ---------------------------------------------------------------------------

def test_row_level_filter_is_injected_into_sql(env):
    """令牌带 regions=['华东'] -> 生成的 SQL 必须被注入区域过滤。"""
    token = env["auth"].issue("east-manager", Role.ANALYST, regions=["华东"])
    r = env["client"].post(
        "/v1/ask", json={"question": "各实验室设备利用率"}, headers=_h(token)
    )
    body = r.json()
    assert r.status_code == 200 and body["type"] == "result"
    assert "l.region = '华东'" in body["sql"]
    assert any("行级过滤" in s for s in body["data_scope"])


def test_unrestricted_user_has_no_row_filter(env):
    r = env["client"].post(
        "/v1/ask", json={"question": "各实验室设备利用率"}, headers=_h(env["token"]())
    )
    assert "region =" not in r.json()["sql"]
    assert r.json()["data_scope"] == []


def test_metric_denied_by_policy_returns_denied_payload():
    """指标级权限：拒绝应是一条明确的业务提示，而不是一条被拦的 SQL。"""
    policy = DataPolicy(
        allowed_metrics=frozenset({"on_time_completion_rate"}), dialect="postgres"
    )
    settings = make_settings()
    app = create_app(service=make_service(settings, policy_factory=lambda p: policy),
                     settings=settings)
    auth = Authenticator(settings.auth)
    token = auth.issue("alice", Role.ANALYST)
    with TestClient(app) as client:
        r = client.post(
            "/v1/ask", json={"question": "华东区上个月可靠性试验的检测服务收入是多少"},
            headers=_h(token),
        )
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "denied"
    assert "无权查询指标" in body["denied_reason"]


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

def test_audit_records_queries(env):
    client = env["client"]
    client.post("/v1/ask", json={"question": "华东区准时率"}, headers=_h(env["token"]("alice")))
    r = client.get("/v1/audit", headers=_h(env["token"]("ops", Role.ADMIN)))
    records = r.json()["records"]
    assert records and records[0]["sub"] == "alice"
    assert records[0]["question"] == "华东区准时率"
    assert records[0]["outcome"] == "result"
