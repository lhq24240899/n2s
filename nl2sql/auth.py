"""身份与授权：JWT 签发/校验 + 角色到权限点（scope）的映射。

设计要点（为什么这么做）：

1. **角色 -> scope 是唯一事实来源**。接口层只声明「需要哪个 scope」，
   不写 `if role == "admin"`，这样加角色/改权限只动一张表。

2. **认证（Authentication）与授权（Authorization）分离**：
   本模块只回答"你是谁、你能做什么动作"（scope 级）；
   "你能看哪些数据"（表/列/指标/行）在 `policy.py`，两者互不耦合。

3. **令牌自带身份，服务端无状态**。JWT 里放 sub/role/scopes/数据可见范围，
   API 与 MCP server 用同一个校验函数，不必共享会话存储。

4. **生产必须注入密钥**。`AUTH__SECRET` 为空时：
   - 若开了 `AUTH__DEV_TOKEN_ENDPOINT`（本地演示）-> 随机生成并打警告；
   - 否则**直接抛错**，避免"忘记配密钥就上线"这种最危险的情况。

命令行签发（本地演示用）：
    python -m nl2sql.auth --role analyst --sub alice --ttl 7200
"""
from __future__ import annotations

import argparse
import logging
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional

ALGORITHM = "HS256"
ISSUER = "nl2sql"
AUDIENCE = "nl2sql-agent"
_log = logging.getLogger("nl2sql.auth")


# ---------------------------------------------------------------------------
# 角色与权限点
# ---------------------------------------------------------------------------

class Role(str, Enum):
    """对外只暴露三种角色，够用且好解释。"""

    VIEWER = "viewer"      # 只看：能看指标目录/知识库，不能问数
    ANALYST = "analyst"    # 问数：默认角色，可问数，但受数据权限约束
    ADMIN = "admin"        # 管理：额外可看审计日志等


class Scope(str, Enum):
    """动作级权限点。接口按 scope 声明，不按角色声明。"""

    SCHEMA_READ = "schema:read"
    KB_READ = "kb:read"
    QUERY_ASK = "query:ask"
    AUDIT_READ = "audit:read"


ROLE_SCOPES: dict[Role, frozenset[Scope]] = {
    Role.VIEWER: frozenset({Scope.SCHEMA_READ, Scope.KB_READ}),
    Role.ANALYST: frozenset({Scope.SCHEMA_READ, Scope.KB_READ, Scope.QUERY_ASK}),
    Role.ADMIN: frozenset(
        {Scope.SCHEMA_READ, Scope.KB_READ, Scope.QUERY_ASK, Scope.AUDIT_READ}
    ),
}


class AuthError(Exception):
    """认证失败基类。接口层据此返回 401。"""

    status_code = 401


class InvalidToken(AuthError):
    pass


class TokenExpired(AuthError):
    pass


class MissingSecret(AuthError):
    """服务端未配置密钥——属于部署错误，接口层返回 503。"""

    status_code = 503


# ---------------------------------------------------------------------------
# 主体（Principal）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Principal:
    """一次请求的身份：谁、什么角色、能做什么、能看到哪些数据。

    business_lines / regions 为 None 表示"不限制"；否则只允许这些取值。
    这两个字段是"数据权限"从令牌到策略层的传递通道。
    """

    sub: str
    role: Role
    scopes: frozenset[Scope]
    business_lines: frozenset[str] | None = None
    regions: frozenset[str] | None = None
    exp: int = 0
    extra: dict = field(default_factory=dict)

    @classmethod
    def of(
        cls,
        sub: str,
        role: Role | str,
        *,
        business_lines: Iterable[str] | None = None,
        regions: Iterable[str] | None = None,
        exp: int = 0,
        extra: dict | None = None,
    ) -> "Principal":
        r = Role(role)
        return cls(
            sub=sub,
            role=r,
            scopes=ROLE_SCOPES[r],
            business_lines=frozenset(business_lines) if business_lines else None,
            regions=frozenset(regions) if regions else None,
            exp=exp,
            extra=extra or {},
        )

    def has(self, scope: Scope) -> bool:
        return scope in self.scopes

    @property
    def is_admin(self) -> bool:
        return self.role is Role.ADMIN

    def to_dict(self) -> dict:
        return {
            "sub": self.sub,
            "role": self.role.value,
            "scopes": sorted(s.value for s in self.scopes),
            "business_lines": sorted(self.business_lines) if self.business_lines else None,
            "regions": sorted(self.regions) if self.regions else None,
            "exp": self.exp,
        }


# ---------------------------------------------------------------------------
# JWT 签发 / 校验
# ---------------------------------------------------------------------------

def _jwt():
    try:
        import jwt  # PyJWT

        return jwt
    except ImportError as e:  # pragma: no cover - 依赖缺失时给出清晰指引
        raise MissingSecret(
            "未安装 PyJWT，无法使用鉴权。请先 `pip install pyjwt`。"
        ) from e


def issue_token(
    subject: str,
    role: Role | str,
    secret: str,
    *,
    ttl_seconds: int = 3600,
    business_lines: Iterable[str] | None = None,
    regions: Iterable[str] | None = None,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
) -> str:
    """签发访问令牌。数据可见范围直接写进令牌，服务端无状态即可鉴权。"""
    if not secret:
        raise MissingSecret("签发令牌需要 AUTH__SECRET（生产用密码学随机值）")
    jwt = _jwt()
    now = int(time.time())
    payload = {
        "sub": subject,
        "role": Role(role).value,
        "scopes": sorted(s.value for s in ROLE_SCOPES[Role(role)]),
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    if business_lines:
        payload["business_lines"] = sorted(business_lines)
    if regions:
        payload["regions"] = sorted(regions)
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_token(
    token: str,
    secret: str,
    *,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
) -> Principal:
    """校验令牌并还原身份。失败抛 AuthError 子类（接口层映射为 401/503）。"""
    if not secret:
        raise MissingSecret("未配置 AUTH__SECRET，无法校验令牌")
    jwt = _jwt()
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],   # 固定算法，防 "alg=none" 类降级攻击
            issuer=issuer,
            audience=audience,
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise TokenExpired("令牌已过期，请重新获取") from e
    except Exception as e:  # noqa: BLE001 - 统一收敛为认证失败
        raise InvalidToken(f"令牌无效: {e}") from e

    role_raw = claims.get("role") or Role.ANALYST.value
    try:
        role = Role(role_raw)
    except ValueError as e:
        raise InvalidToken(f"未知角色: {role_raw}") from e

    # 权限一律以**服务端映射**为准，忽略令牌里的 scopes 声明。
    # 这样即使令牌被签成了越权内容（签发服务被攻破或配置错误），
    # 校验端也不会跟着一起越权——"不信任客户端声明"的第二层含义。
    return Principal(
        sub=str(claims["sub"]),
        role=role,
        scopes=ROLE_SCOPES[role],
        business_lines=(
            frozenset(claims["business_lines"]) if claims.get("business_lines") else None
        ),
        regions=frozenset(claims["regions"]) if claims.get("regions") else None,
        exp=int(claims.get("exp") or 0),
    )


# ---------------------------------------------------------------------------
# 服务端鉴权器
# ---------------------------------------------------------------------------

class Authenticator:
    """把 `AuthSettings` 变成可用的签发/校验入口（接口层只依赖它）。"""

    def __init__(self, settings):
        self.enabled = bool(getattr(settings, "enabled", True))
        self.issuer = getattr(settings, "issuer", ISSUER)
        self.audience = getattr(settings, "audience", AUDIENCE)
        self.ttl_seconds = int(getattr(settings, "ttl_seconds", 3600))
        self.dev_token_endpoint = bool(getattr(settings, "dev_token_endpoint", False))
        secret = (getattr(settings, "secret", "") or "").strip()

        if self.enabled and not secret:
            if self.dev_token_endpoint:
                # 本地/演示：随机密钥 + 高亮警告。重启后旧令牌失效（可接受）。
                secret = secrets.token_urlsafe(32)
                _log.warning(
                    "AUTH__SECRET 未配置：已生成临时随机密钥（仅本次进程有效）。"
                    "生产环境请注入固定密钥，否则重启即全员掉线。"
                )
            else:
                raise MissingSecret(
                    "AUTH__SECRET 未配置且未开启 dev token 端点，拒绝启动鉴权服务。"
                    "请设置 AUTH__SECRET，或本地演示时设 AUTH__DEV_TOKEN_ENDPOINT=true。"
                )
        self.secret = secret

    def issue(self, subject: str, role: Role | str, **kw) -> str:
        return issue_token(
            subject,
            role,
            self.secret,
            ttl_seconds=kw.pop("ttl_seconds", self.ttl_seconds),
            issuer=self.issuer,
            audience=self.audience,
            **kw,
        )

    def principal(self, token: str) -> Principal:
        return decode_token(token, self.secret, issuer=self.issuer, audience=self.audience)

    def anonymous(self) -> Principal:
        """鉴权关闭时的兜底身份：本地开发默认给 analyst（仍受只读与数据权限约束）。"""
        return Principal.of("anonymous", Role.ANALYST)


# ---------------------------------------------------------------------------
# CLI：签发令牌（本地演示用）
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="签发 NL2SQL 访问令牌")
    parser.add_argument("--sub", default="demo", help="用户名/主体标识")
    parser.add_argument(
        "--role", default=Role.ANALYST.value, choices=[r.value for r in Role]
    )
    parser.add_argument("--ttl", type=int, default=3600, help="有效期（秒）")
    parser.add_argument("--regions", default="", help="限定区域，逗号分隔（留空=不限）")
    parser.add_argument("--business-lines", default="", help="限定业务线 code，逗号分隔")
    args = parser.parse_args(argv)

    from .config import get_settings

    settings = get_settings()
    auth = Authenticator(settings.auth)
    token = auth.issue(
        args.sub,
        args.role,
        ttl_seconds=args.ttl,
        regions=[s for s in args.regions.split(",") if s] or None,
        business_lines=[s for s in args.business_lines.split(",") if s] or None,
    )
    print(token)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
