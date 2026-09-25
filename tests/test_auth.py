"""鉴权与授权单测：令牌签发/校验、角色→scope、过期与篡改、数据范围。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nl2sql.auth import (
    Authenticator,
    InvalidToken,
    MissingSecret,
    Principal,
    Role,
    Scope,
    TokenExpired,
    decode_token,
    issue_token,
)
from nl2sql.config import AuthSettings

SECRET = "unit-test-secret-please-rotate-0123456789abcdef"  # >=32 字节，避免 PyJWT 弱密钥告警
ISS = "nl2sql"
AUD = "nl2sql-agent"


def test_issue_and_decode_roundtrip():
    token = issue_token("alice", Role.ANALYST, SECRET)
    p = decode_token(token, SECRET)
    assert p.sub == "alice"
    assert p.role is Role.ANALYST
    assert p.has(Scope.QUERY_ASK)
    assert not p.has(Scope.AUDIT_READ)  # analyst 不得读审计


def test_role_scopes_are_enforced_from_server_side_mapping():
    """令牌里若伪造了 scopes，仍以服务端映射为准（不信任客户端声明）。"""
    import jwt as pyjwt

    token = pyjwt.encode(
        {
            "sub": "mallory", "role": "viewer", "scopes": ["audit:read", "query:ask"],
            "iss": ISS, "aud": AUD, "exp": 9999999999,
        },
        SECRET,
        algorithm="HS256",
    )
    p = decode_token(token, SECRET)
    assert p.role is Role.VIEWER
    assert not p.has(Scope.AUDIT_READ)
    assert p.has(Scope.KB_READ)  # viewer 的合法权限仍在


def test_expired_token_rejected():
    token = issue_token("alice", Role.ANALYST, SECRET, ttl_seconds=-10)
    with pytest.raises(TokenExpired):
        decode_token(token, SECRET)


def test_tampered_token_rejected():
    token = issue_token("alice", Role.VIEWER, SECRET)
    tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    with pytest.raises(InvalidToken):
        decode_token(tampered, SECRET)


def test_wrong_secret_rejected():
    token = issue_token("alice", Role.ANALYST, SECRET)
    with pytest.raises(InvalidToken):
        decode_token(token, "another-secret")


def test_wrong_audience_rejected():
    token = issue_token("alice", Role.ANALYST, SECRET, audience="someone-else")
    with pytest.raises(InvalidToken):
        decode_token(token, SECRET)


def test_data_scope_travels_in_token():
    """数据可见范围随令牌下发——服务端无状态也能做行级权限。"""
    token = issue_token(
        "east-manager", Role.ANALYST, SECRET, regions=["华东"], business_lines=["reliability"]
    )
    p = decode_token(token, SECRET)
    assert p.regions == frozenset({"华东"})
    assert p.business_lines == frozenset({"reliability"})


def test_unknown_role_rejected():
    token = issue_token("alice", Role.VIEWER, SECRET)
    # 伪造一个不存在的角色
    import jwt as pyjwt

    forged = pyjwt.encode(
        {"sub": "a", "role": "superuser", "iss": ISS, "aud": AUD, "exp": 9999999999},
        SECRET, algorithm="HS256",
    )
    with pytest.raises(InvalidToken):
        decode_token(forged, SECRET)
    assert decode_token(token, SECRET).role is Role.VIEWER


def test_authenticator_requires_secret_in_production():
    """未配密钥且未开 dev 端点 -> 拒绝启动（避免"忘了配密钥就上线"）。"""
    with pytest.raises(MissingSecret):
        Authenticator(AuthSettings(secret="", dev_token_endpoint=False))


def test_authenticator_generates_dev_secret_with_warning():
    auth = Authenticator(AuthSettings(secret="", dev_token_endpoint=True))
    token = auth.issue("dev", Role.ADMIN)
    assert auth.principal(token).role is Role.ADMIN


def test_anonymous_principal_is_readonly_analyst():
    p = Principal.of("anonymous", Role.ANALYST)
    assert p.has(Scope.QUERY_ASK) and not p.has(Scope.AUDIT_READ)
