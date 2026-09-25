"""智能体 API（FastAPI）：把问数能力开放给前端、其它智能体与 MCP 客户端。

四道门（每一道都有明确职责，不重复也不留缺口）：

  1. **认证**  Bearer JWT -> 你是谁。缺失/过期/伪造 = 401；未配密钥 = 503。
  2. **授权**  scope 声明式校验，接口只写「需要哪个 scope」，不写角色判断。
  3. **数据权限**  角色 + 令牌里的可见范围 -> `DataPolicy`：
     生成前隐藏无权表、指标级拒绝，生成后列级拦截 + **行级过滤注入 SQL**。
  4. **执行安全**  sqlglot AST 白名单 + EXPLAIN 预检 + **数据库会话级只读**。
     前三道都在应用层，最后一道在数据库层——即使应用层被绕过，写操作也进不去。

为什么把同步路由写成 `def` 而不是 `async def`：
  引擎内部是同步阻塞的（openai SDK、psycopg 都是同步客户端）。
  写成 `async def` 会**阻塞事件循环**，把整台服务拖死；
  写成普通 `def` 时 FastAPI 会自动丢进线程池，再配合 `ConcurrencyGate`
  限制线程池里真正打到 LLM 的并发——这才是这套栈里正确的并发模型。

启动：
    uvicorn "nl2sql.api:create_app" --factory --host 0.0.0.0 --port 8000
    （或 python examples/api_server.py）
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth import AuthError, Authenticator, MissingSecret, Principal, Role, Scope
from .config import Settings, get_settings
from .service import (
    QueryService,
    RateLimited,
    ServiceError,
    SessionForbidden,
    SessionNotFound,
    Busy,
)

_log = logging.getLogger("nl2sql.api")


# ---------------------------------------------------------------------------
# 请求 / 响应模型（写出来主要是为了 /docs 上的接口契约看得见）
# ---------------------------------------------------------------------------

class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500, description="自然语言问题")
    session_id: Optional[str] = Field(
        None, max_length=64,
        description="会话 ID：同一 ID 内多轮上下文继承；不传则用 <用户>:default",
    )


class AskResponse(BaseModel):
    request_id: str
    type: str = Field(..., description="result | rag | clarification | denied")
    answer: Optional[str] = None
    metric: Optional[str] = None
    columns: list[str] = []
    rows: list[list[Any]] = []
    row_count: int = 0
    sql: Optional[str] = Field(None, description="实际执行的 SQL（含行级过滤改写）")
    source: Optional[str] = None
    entities: dict[str, Any] = {}
    reasons: list[str] = Field([], description="可解释性：语义映射/权限决策的依据")
    citations: list[dict[str, Any]] = []
    data_scope: list[str] = Field([], description="本次查询生效的数据权限说明")
    denied_reason: Optional[str] = None
    error: Optional[str] = None
    empty: Optional[bool] = None
    latency_ms: float = 0.0


class TokenRequest(BaseModel):
    sub: str = Field("demo", max_length=64)
    role: Role = Role.ANALYST
    ttl_seconds: Optional[int] = Field(None, ge=30, le=86400)
    regions: Optional[list[str]] = None
    business_lines: Optional[list[str]] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    principal: dict[str, Any]


class KBRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    top_k: int = Field(4, ge=1, le=10)


# ---------------------------------------------------------------------------
# 认证 / 授权依赖
# ---------------------------------------------------------------------------

def _resolve_principal(request: Request) -> Principal:
    auth: Authenticator = request.app.state.authenticator
    if not auth.enabled:
        return auth.anonymous()
    raw = request.headers.get("authorization") or ""
    if not raw.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="缺少 Bearer Token（Authorization: Bearer <token>）",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return auth.principal(raw.split(" ", 1)[1].strip())
    except MissingSecret as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except AuthError as e:
        raise HTTPException(
            status_code=401, detail=str(e), headers={"WWW-Authenticate": "Bearer"}
        ) from e


def require(scope: Scope):
    """声明式权限依赖：接口只声明 scope，角色到 scope 的映射在 auth.py 一处维护。"""

    def _dep(request: Request) -> Principal:
        principal = _resolve_principal(request)
        if not principal.has(scope):
            raise HTTPException(
                status_code=403,
                detail=f"当前角色 {principal.role.value} 缺少权限：{scope.value}",
            )
        return principal

    return _dep


def _service(request: Request) -> QueryService:
    svc: Optional[QueryService] = getattr(request.app.state, "service", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail=f"服务未就绪：{getattr(request.app.state, 'service_error', '未知原因')}",
        )
    return svc


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------

def create_app(
    service: Optional[QueryService] = None,
    settings: Optional[Settings] = None,
    authenticator: Optional[Authenticator] = None,
) -> FastAPI:
    settings = settings or get_settings()
    api_cfg = settings.api

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动时装配一次，所有请求复用（上下文无状态，可安全共享）
        if service is not None:
            app.state.service = service
        else:
            try:
                from .bootstrap import build_service

                app.state.service, app.state.context = build_service(settings)
                app.state.service_error = None
            except Exception as e:  # noqa: BLE001 - 服务仍要能起，便于排障
                _log.exception("服务装配失败")
                app.state.service = None
                app.state.service_error = str(e)[:300]
        yield
        svc = getattr(app.state, "service", None)
        for closer in ("close",):
            fn = getattr(getattr(svc, "doc_retriever", None), closer, None)
            if callable(fn):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass

    app = FastAPI(
        title=api_cfg.title,
        version=api_cfg.version,
        description=(
            "语义层驱动的 NL2SQL 智能体 API：混合 RAG + 可解释检索 + 只读数据权限。\n\n"
            "**鉴权**：`Authorization: Bearer <JWT>`；本地演示可用 `POST /v1/auth/token` 自助签发。"
        ),
        lifespan=lifespan,
    )
    app.state.authenticator = authenticator or Authenticator(settings.auth)
    if service is not None:
        app.state.service = service

    # ---- 统一错误体：{"error": {...}}，前端不必解析框架默认的 {"detail": ...} ----
    @app.exception_handler(ServiceError)
    async def _service_error_handler(_: Request, exc: ServiceError):
        payload: dict[str, Any] = {
            "error": {"type": type(exc).__name__, "message": str(exc)}
        }
        headers = {}
        if isinstance(exc, (RateLimited, Busy)):
            retry = getattr(exc, "retry_after", 0)
            payload["error"]["retry_after"] = retry
            headers["Retry-After"] = str(int(retry) + 1)
        return JSONResponse(status_code=exc.status_code, content=payload, headers=headers)

    # ---------------- 无鉴权 ----------------

    @app.get("/", include_in_schema=False)
    def root():
        return {
            "service": api_cfg.title,
            "version": api_cfg.version,
            "docs": "/docs",
            "endpoints": [
                "/health", "/v1/whoami", "/v1/schema", "/v1/ask",
                "/v1/kb/search", "/v1/sessions/{session_id}/reset", "/v1/audit",
            ],
        }

    @app.get("/health", tags=["运维"], summary="健康探测（无需鉴权）")
    def health(request: Request):
        svc = getattr(request.app.state, "service", None)
        if svc is None:
            return JSONResponse(
                status_code=503,
                content={"status": "down", "reason": getattr(request.app.state, "service_error", "")},
            )
        return {"status": "ok", **svc.health()}

    @app.post(
        "/v1/auth/token",
        response_model=TokenResponse,
        tags=["认证"],
        summary="签发令牌（仅本地演示：需 AUTH__DEV_TOKEN_ENDPOINT=true）",
    )
    def issue_token(body: TokenRequest, request: Request):
        auth: Authenticator = request.app.state.authenticator
        if not (auth.dev_token_endpoint and auth.enabled):
            raise HTTPException(
                status_code=404,
                detail="未开启开发用令牌端点（生产环境应接入企业 IdP / SSO）",
            )
        token = auth.issue(
            body.sub, body.role,
            ttl_seconds=body.ttl_seconds or auth.ttl_seconds,
            regions=body.regions, business_lines=body.business_lines,
        )
        principal = auth.principal(token)
        return TokenResponse(
            access_token=token,
            expires_in=body.ttl_seconds or auth.ttl_seconds,
            principal=principal.to_dict(),
        )

    # ---------------- 已鉴权 ----------------

    @app.get("/v1/whoami", tags=["认证"], summary="查看当前身份与权限")
    def whoami(principal: Annotated[Principal, Depends(_resolve_principal)]):
        return principal.to_dict()

    @app.get("/v1/schema", tags=["元数据"], summary="可见的表 / 指标 / 数据范围（按角色过滤）")
    def schema(
        request: Request,
        principal: Annotated[Principal, Depends(require(Scope.SCHEMA_READ))],
    ):
        return _service(request).schema_catalog(principal)

    @app.post("/v1/kb/search", tags=["知识库"], summary="企业知识库混合检索（带可解释依据）")
    def kb_search(
        body: KBRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require(Scope.KB_READ))],
    ):
        return _service(request).kb_search(body.query, principal, top_k=body.top_k)

    @app.post(
        "/v1/ask",
        response_model=AskResponse,
        tags=["问数"],
        summary="自然语言问数（结构化 SQL / 文档 RAG / 澄清 / 拒绝 四种结果）",
    )
    def ask(
        body: AskRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require(Scope.QUERY_ASK))],
    ):
        return _service(request).ask(body.question, principal, session_id=body.session_id)

    @app.post("/v1/sessions/{session_id}/reset", tags=["问数"], summary="重置会话多轮上下文")
    def reset_session(
        session_id: str,
        request: Request,
        principal: Annotated[Principal, Depends(require(Scope.QUERY_ASK))],
    ):
        return _service(request).reset(session_id, principal)

    @app.get("/v1/audit", tags=["运维"], summary="近期审计记录（需 audit:read）")
    def audit(
        request: Request,
        principal: Annotated[Principal, Depends(require(Scope.AUDIT_READ))],
        limit: int = 50,
    ):
        return {"records": _service(request).audit_recent(min(max(limit, 1), 200))}

    return app
