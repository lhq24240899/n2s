"""应用服务层：会话管理、限流、并发闸门、审计、响应序列化。

刻意**不导入 FastAPI**。原因有三：
1. 同一套能力要被三个入口复用：HTTP API（api.py）、MCP server（mcp_server.py）、
   Streamlit（streamlit_app.py）；
2. 单测可以不起 HTTP 就覆盖全部业务逻辑（快且稳定）；
3. 换 Web 框架（或加 gRPC）时这层不动——这是分层最实际的收益。

四个生产必备件：
- **会话**：每个会话一个引擎实例（内含独立多轮上下文），带 TTL 与容量上限。
  不做这两件事的 Agent 服务，跑一夜就会把内存吃掉——非常常见的线上事故。
- **限流**：按主体（sub）的令牌桶。防止某个调用方把 LLM 预算刷干。
- **并发闸门**：有界信号量限制"同时打到 LLM/DB 的请求数"，超时即拒绝。
  这就是"算力调度"最朴素也最有效的形态——保护下游，而不是让它雪崩。
- **审计**：谁、在什么时候、问了什么、生成了什么 SQL、返回多少行、是否被拒绝。
  只读系统同样需要审计：数据泄露往往发生在"合法查询"里。
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from decimal import Decimal
from typing import Any, Callable, Optional

from .auth import Principal
from .policy import DataPolicy

_log = logging.getLogger("nl2sql.service")


# ---------------------------------------------------------------------------
# 异常（接口层按 status_code 映射 HTTP 状态码）
# ---------------------------------------------------------------------------

class ServiceError(Exception):
    status_code = 500


class RateLimited(ServiceError):
    status_code = 429

    def __init__(self, reason: str, retry_after: float = 60.0):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class Busy(ServiceError):
    """并发已满且排队超时。用 429 而不是 503：语义是"稍后重试"。"""

    status_code = 429

    def __init__(self, reason: str, retry_after: float = 5.0):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class SessionForbidden(ServiceError):
    status_code = 403


class SessionNotFound(ServiceError):
    status_code = 404


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------

def jsonable(value: Any) -> Any:
    """把数据库返回的 Decimal / date / datetime 等转成可 JSON 化的类型。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        # 金额类保留两位小数；整数值用 int，避免前端显示 3200000.0
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    return str(value)


# ---------------------------------------------------------------------------
# 限流 / 并发
# ---------------------------------------------------------------------------

class TokenBucket:
    """按主体的令牌桶限流（线程安全）。"""

    def __init__(self, rate_per_min: int, capacity: Optional[int] = None):
        self.rate = max(rate_per_min, 1) / 60.0     # 每秒补充
        self.capacity = float(capacity or max(rate_per_min, 1))
        self._state: dict[str, tuple[float, float]] = {}  # sub -> (tokens, last_ts)
        self._lock = threading.Lock()

    def consume(self, key: str, n: float = 1.0) -> tuple[bool, float]:
        """返回 (是否放行, 建议重试等待秒数)。"""
        now = time.monotonic()
        with self._lock:
            tokens, last = self._state.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < n:
                need = (n - tokens) / self.rate
                self._state[key] = (tokens, now)
                return False, round(need, 2)
            self._state[key] = (tokens - n, now)
            return True, 0.0


class ConcurrencyGate:
    """有界信号量：限制同时执行的查询数，超时即拒绝（保护 LLM 与数据库）。"""

    def __init__(self, limit: int, timeout: float = 20.0):
        self.limit = max(int(limit), 1)
        self.timeout = timeout
        self._sem = threading.BoundedSemaphore(self.limit)
        self.in_flight = 0
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def slot(self):
        acquired = self._sem.acquire(timeout=self.timeout)
        if not acquired:
            raise Busy(
                f"服务繁忙（并发上限 {self.limit}），请稍后重试",
                retry_after=max(self.timeout / 4, 1.0),
            )
        with self._lock:
            self.in_flight += 1
        try:
            yield
        finally:
            with self._lock:
                self.in_flight -= 1
            self._sem.release()

    def snapshot(self) -> dict:
        with self._lock:
            return {"limit": self.limit, "in_flight": self.in_flight}


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

@dataclass
class Session:
    """一个会话 = 一个引擎实例（内含独立的多轮上下文，互不串台）。"""

    id: str
    owner: str
    role: str
    engine: Any
    policy: DataPolicy
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    turns: int = 0


class SessionStore:
    """会话存储：TTL 过期 + 容量上限（LRU 淘汰）+ 归属校验。

    归属校验很重要：会话 ID 由客户端提交，若不校验 owner，
    换个 session_id 就能接管别人的多轮上下文（越权读历史问题/口径）。
    """

    def __init__(
        self,
        factory: Callable[[Principal], Any],
        ttl_seconds: int = 1800,
        max_sessions: int = 200,
    ):
        self._factory = factory
        self.ttl = ttl_seconds
        self.max_sessions = max_sessions
        self._items: dict[str, Session] = {}
        self._lock = threading.Lock()

    def get(self, sid: str, principal: Principal, policy: DataPolicy) -> Session:
        now = time.time()
        with self._lock:
            self._evict_locked(now)
            s = self._items.get(sid)
            if s is None:
                s = Session(
                    id=sid,
                    owner=principal.sub,
                    role=principal.role.value,
                    engine=self._factory(principal),
                    policy=policy,
                )
                self._items[sid] = s
            elif s.owner != principal.sub:
                raise SessionForbidden(
                    f"会话 {sid} 属于其它用户，不能接管（换一个 session_id 或省略该字段）"
                )
            s.last_used = now
            return s

    def reset(self, sid: str, principal: Principal) -> Session:
        with self._lock:
            s = self._items.get(sid)
        if s is None:
            raise SessionNotFound(f"会话 {sid} 不存在或已过期")
        if s.owner != principal.sub:
            raise SessionForbidden(f"会话 {sid} 属于其它用户")
        s.engine.reset_context()
        s.turns = 0
        s.last_used = time.time()
        return s

    def drop(self, sid: str) -> None:
        with self._lock:
            self._items.pop(sid, None)

    def _evict_locked(self, now: float) -> None:
        expired = [k for k, v in self._items.items() if now - v.last_used > self.ttl]
        for k in expired:
            self._items.pop(k, None)
        if len(self._items) > self.max_sessions:
            # 超出容量：淘汰最久未使用
            for k, _ in sorted(self._items.items(), key=lambda kv: kv[1].last_used)[
                : len(self._items) - self.max_sessions
            ]:
                self._items.pop(k, None)

    def stats(self) -> dict:
        with self._lock:
            return {"sessions": len(self._items), "capacity": self.max_sessions, "ttl": self.ttl}


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

class AuditLog:
    """审计：结构化日志 + 内存环形缓冲（供 /v1/audit 查看近期记录）。"""

    def __init__(self, buffer: int = 500):
        self._buf: deque[dict] = deque(maxlen=buffer)

    def record(self, **fields) -> dict:
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), **jsonable(fields)}
        self._buf.append(entry)
        _log.info("AUDIT %s", json.dumps(entry, ensure_ascii=False))
        return entry

    def recent(self, limit: int = 50) -> list[dict]:
        return list(self._buf)[-limit:][::-1]


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------

class QueryService:
    """把引擎能力包装成"可被多入口复用"的服务。

    仅为可选的依赖（registry / layer / db / doc_retriever）都可以不传，
    传了才启用 `/v1/schema`、健康探测、知识库检索等能力——
    这样单测可以用最小依赖跑通主流程。
    """

    def __init__(
        self,
        engine_factory: Callable[..., Any],
        *,
        settings=None,
        registry=None,
        layer=None,
        db=None,
        doc_retriever=None,
        policy_factory: Optional[Callable[[Principal], DataPolicy]] = None,
    ):
        api = getattr(settings, "api", None)
        self.settings = settings
        self.registry = registry
        self.layer = layer
        self.db = db
        self.doc_retriever = doc_retriever
        self.policy_factory = policy_factory or (
            lambda p: DataPolicy.from_principal(p, dialect=getattr(getattr(settings, "db", None), "dialect", "postgres"))
        )
        self.sessions = SessionStore(
            factory=lambda p: engine_factory(p, self.policy_factory(p)),
            ttl_seconds=int(getattr(api, "session_ttl_seconds", 1800)),
            max_sessions=int(getattr(api, "max_sessions", 200)),
        )
        self.limiter = TokenBucket(int(getattr(api, "rate_limit_per_min", 30)))
        self.gate = ConcurrencyGate(
            int(getattr(api, "max_concurrency", 4)),
            float(getattr(api, "queue_timeout", 20.0)),
        )
        self.audit = AuditLog(int(getattr(api, "audit_buffer", 500)))

    # ---------------- 主入口 ----------------

    def ask(
        self,
        question: str,
        principal: Principal,
        session_id: Optional[str] = None,
    ) -> dict:
        request_id = uuid.uuid4().hex[:12]
        allowed, retry_after = self.limiter.consume(principal.sub)
        if not allowed:
            self.audit.record(
                request_id=request_id, sub=principal.sub, role=principal.role.value,
                question=question, outcome="rate_limited",
            )
            raise RateLimited("请求过于频繁，请稍后重试", retry_after=retry_after)

        sid = session_id or f"{principal.sub}:default"
        started = time.perf_counter()
        with self.gate.slot():
            session = self.sessions.get(sid, principal, self.policy_factory(principal))
            guard = getattr(session.engine, "guard", None)
            if guard is not None:
                guard.applied.clear()
            try:
                out = session.engine.ask(question)
            except Exception as e:  # noqa: BLE001 - 交给上层统一映射为 503
                self.audit.record(
                    request_id=request_id, sub=principal.sub, role=principal.role.value,
                    session=sid, question=question, outcome="error", error=str(e)[:300],
                )
                raise ServiceError(f"查询失败: {e}") from e
            session.turns += 1
            latency_ms = round((time.perf_counter() - started) * 1000, 1)

            payload = self._serialize(out, request_id, latency_ms, guard)
            self.audit.record(
                request_id=request_id, sub=principal.sub, role=principal.role.value,
                session=sid, question=question, outcome=payload["type"],
                sql=payload.get("sql"), rows=payload.get("row_count"),
                metric=payload.get("metric"), latency_ms=latency_ms,
                denied=payload.get("denied_reason"), data_scope=payload.get("data_scope"),
            )
            return payload

    def reset(self, session_id: str, principal: Principal) -> dict:
        self.sessions.reset(session_id, principal)
        self.audit.record(
            request_id=uuid.uuid4().hex[:12], sub=principal.sub,
            role=principal.role.value, session=session_id, outcome="reset_session",
        )
        return {"session_id": session_id, "reset": True}

    # ---------------- 只读能力 ----------------

    def schema_catalog(self, principal: Principal) -> dict:
        """按数据权限过滤后的"你能看到什么"——门户口径的可视化入口。"""
        policy = self.policy_factory(principal)
        tables = []
        for name, t in getattr(self.registry, "tables", {}).items():
            if policy.allowed_tables is not None and name not in policy.allowed_tables:
                continue
            cols = [
                {"name": c, "type": ty, "sensitive": c in policy.denied_columns}
                for c, ty in t.columns.items()
            ]
            tables.append({"name": name, "description": t.description, "columns": cols})
        metrics = []
        for mid, m in getattr(self.layer, "metrics", {}).items():
            if policy.allowed_metrics is not None and mid not in policy.allowed_metrics:
                continue
            metrics.append(
                {
                    "id": m.id, "name": m.name, "level": m.level, "domain": m.domain,
                    "definition": m.definition, "dimensions": list(m.dimensions),
                    "source_tables": list(m.source_tables),
                }
            )
        return {
            "role": principal.role.value,
            "sub": principal.sub,
            "data_scope": policy.describe(),
            "tables": tables,
            "metrics": metrics,
        }

    def kb_search(self, query: str, principal: Principal, top_k: int = 4) -> dict:
        if self.doc_retriever is None:
            return {"query": query, "hits": [], "note": "知识库未启用"}
        docs = self.doc_retriever.retrieve(query)[:top_k]
        return {
            "query": query,
            "hits": [
                {
                    "id": d.id, "title": d.title, "source": d.source,
                    "score": jsonable(d.score), "reasons": list(d.reasons),
                    "content": d.content,
                }
                for d in docs
            ],
        }

    def health(self) -> dict:
        """健康探测：不强依赖 LLM（避免每次探测都产生费用），只探 DB 与知识库。"""
        db_ok, db_detail = False, "未配置"
        if self.db is not None:
            try:
                self.db.execute("SELECT 1")
                db_ok, db_detail = True, "ok"
            except Exception as e:  # noqa: BLE001
                db_detail = str(e)[:200]
        return {
            "status": "ok" if (db_ok or self.db is None) else "degraded",
            "db": {"ok": db_ok, "detail": db_detail},
            "kb": {"enabled": self.doc_retriever is not None},
            "sessions": self.sessions.stats(),
            "concurrency": self.gate.snapshot(),
        }

    def audit_recent(self, limit: int = 50) -> list[dict]:
        return self.audit.recent(limit)

    # ---------------- 序列化 ----------------

    def _serialize(self, out: dict, request_id: str, latency_ms: float, guard) -> dict:
        kind = out.get("type", "unknown")
        base: dict = {
            "request_id": request_id,
            "type": kind,
            "latency_ms": latency_ms,
            "data_scope": list(getattr(guard, "applied", []) or []),
        }
        mapped = out.get("mapped")
        if mapped is not None:
            base["metric"] = mapped.metric.name if mapped.metric else None
            base["entities"] = jsonable(dict(mapped.entities))
            base["reasons"] = list(mapped.reasons)
            base["normalized_question"] = mapped.normalized

        if kind == "clarification":
            base["answer"] = out.get("message", "")
            return base

        if kind == "denied":
            base["denied_reason"] = out.get("message", "无权访问")
            base["detail"] = out.get("detail", "")
            base["answer"] = out.get("message", "")
            return base

        if kind == "rag":
            docs = out.get("docs") or []
            base["answer"] = out.get("answer", "")
            base["citations"] = [
                {
                    "id": d.id, "title": d.title, "source": d.source,
                    "score": jsonable(d.score), "reasons": list(d.reasons),
                }
                for d in docs
            ]
            base["row_count"] = 0
            return base

        # kind == "result"
        res = out.get("result")
        cols = out.get("cols") or []
        rows = out.get("rows") or []
        base["columns"] = list(cols)
        base["rows"] = [[jsonable(v) for v in r] for r in rows]
        base["row_count"] = len(rows)
        base["sql"] = getattr(res, "sql", None)
        base["source"] = getattr(getattr(res, "source", None), "value", None)
        base["error"] = getattr(res, "error", None)
        base["empty"] = len(rows) == 0 or all(v is None for r in rows for v in r)
        glossary = out.get("glossary")
        if glossary is not None:
            # Glossary.metrics 是 {名称: Metric} 的字典
            base["glossary"] = jsonable(
                [
                    {"name": m.name, "definition": m.definition}
                    for m in (getattr(glossary, "metrics", {}) or {}).values()
                ]
            )
        docs = out.get("docs") or []
        if docs:
            base["citations"] = [
                {"id": d.id, "title": d.title, "source": d.source, "score": jsonable(d.score)}
                for d in docs
            ]
        return base
