"""测试用共享装配：用确定性替身（GRGMockLLM / GRGSampleDB）搭出真实服务栈。

不联网、不花钱、不碰真库，但走的是**真实的** QueryService + 引擎 + 语义层 + 权限层，
因此 API / MCP 的测试覆盖的是真实调用链，而不是 mock 出来的假接口。
"""
from __future__ import annotations

from nl2sql.auth import Role
from nl2sql.config import ApiSettings, AuthSettings, Settings
from nl2sql.pipeline import Text2SQLPipeline
from nl2sql.policy import PolicyGuard
from nl2sql.service import QueryService

from examples.grg_engine import GRGQueryEngine
from examples.grg_schema import build_registry, build_semantic_layer, build_store
from tests.doubles import GRGMockLLM, GRGSampleDB

SECRET = "unit-test-secret-please-rotate-0123456789abcdef"


def make_settings(*, rate_limit: int = 1000, max_concurrency: int = 4,
                  dev_token: bool = True) -> Settings:
    return Settings(
        auth=AuthSettings(secret=SECRET, dev_token_endpoint=dev_token),
        api=ApiSettings(
            rate_limit_per_min=rate_limit,
            max_concurrency=max_concurrency,
            queue_timeout=1.0,
            session_ttl_seconds=1800,
            max_sessions=50,
        ),
    )


def make_service(settings: Settings | None = None, *, policy_factory=None,
                 with_kb: bool = False) -> QueryService:
    settings = settings or make_settings()
    registry = build_registry(settings.db.dialect)
    store = build_store()
    layer = build_semantic_layer()

    def factory(principal, policy):
        guard = PolicyGuard(policy)
        pipeline = Text2SQLPipeline(
            registry=registry, store=store, llm=GRGMockLLM(),
            db=GRGSampleDB(registry, dialect=settings.db.dialect),
            top_k=settings.retrieval.top_k, min_score=settings.retrieval.min_score,
            max_retry=settings.pipeline.max_retry,
        )
        pipeline.guard = guard
        return GRGQueryEngine(pipeline, layer, guard=guard)

    return QueryService(
        factory,
        settings=settings,
        registry=registry,
        layer=layer,
        db=None,
        doc_retriever=_FakeDocRetriever() if with_kb else None,
        policy_factory=policy_factory,
    )


class _FakeDocRetriever:
    """确定性的知识库替身（真实实现是 pgvector + pg_trgm + 关键词三路召回）。"""

    class _Doc:
        def __init__(self, i):
            self.id = f"doc-{i}"
            self.title = f"测试文档 {i}"
            self.content = "EMC 是电磁兼容的缩写……"
            self.source = "内部语料"
            self.score = 0.5
            self.reasons = ["关键词命中 1 个", "RRF 融合得分 0.0492"]

    def retrieve(self, question: str, top_k: int = 4):
        return [self._Doc(i) for i in range(1, min(top_k, 2) + 1)]

    def close(self):
        pass


def admin_token(settings: Settings) -> str:
    from nl2sql.auth import Authenticator

    return Authenticator(settings.auth).issue("ops", Role.ADMIN)
