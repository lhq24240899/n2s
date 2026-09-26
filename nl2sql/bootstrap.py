"""装配层：从 Settings 一次性构建「LLM + DB + 语义层 + 知识库 + 编排」的运行时上下文。

为什么要单独一层？
  api.py / mcp_server.py / streamlit_app.py 都需要同一套装配逻辑；
  各自复制一遍的后果是"三个入口行为不一致"——这是 Agent 项目最常见的腐化方式。

`make_engine_factory` 返回的是**按主体构造引擎**的工厂：
每个会话拿到自己的 pipeline + engine + guard，
所以"多轮上下文"和"权限事实（applied）"都不会跨会话串台。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .auth import Principal
from .config import Settings, get_settings, setup_logging
from .db import build_db
from .es_backend import build_es_backend
from .embedding import build_embedder
from .kb import build_doc_retriever
from .llm import build_llm
from .pipeline import Text2SQLPipeline
from .policy import DataPolicy, PolicyGuard
from .safety import SafetyGuard

_log = logging.getLogger("nl2sql.bootstrap")
_LAST_SCHEMA_FINGERPRINT: str = ""   # 进程内记录上次结构指纹，用于变更告警


@dataclass
class AppContext:
    """运行时上下文：所有入口共享同一份（无状态、可复用）。"""

    settings: Settings
    registry: Any
    store: Any
    layer: Any
    llm: Any = None
    db: Any = None
    embedder: Any = None
    doc_retriever: Any = None


def build_app_context(
    settings: Optional[Settings] = None,
    *,
    need_llm: bool = True,
    need_db: bool = True,
    need_kb: bool = True,
) -> AppContext:
    """构建上下文。领域知识（指标/同义词/示例/知识库语料）来自 examples/；
    表结构来自**元数据接入层**（static=代码内领域库，api=元数据中心同步）。"""
    settings = settings or get_settings()
    setup_logging(settings.log.level, settings.log.fmt)

    from examples.grg_schema import build_semantic_layer, build_store

    from .knowledge import SchemaRegistry
    from .metadata import build_metadata_provider, fingerprint

    # 表结构的单一事实来源：生产切 METADATA__PROVIDER=api 即可，业务代码零改动
    provider = build_metadata_provider(settings)
    tables = provider.load()
    registry = SchemaRegistry(tables, dialect=settings.db.dialect)
    fp = fingerprint(tables)
    global _LAST_SCHEMA_FINGERPRINT
    if _LAST_SCHEMA_FINGERPRINT and _LAST_SCHEMA_FINGERPRINT != fp:
        _log.warning(
            "检测到 schema 变更: %s -> %s（请运行 examples/metadata_check.py 查看明细）",
            _LAST_SCHEMA_FINGERPRINT, fp,
        )
    _LAST_SCHEMA_FINGERPRINT = fp
    _log.info(
        "元数据加载: provider=%s tables=%d fingerprint=%s",
        type(provider).__name__, len(tables), fp,
    )
    store = build_store()
    layer = build_semantic_layer()

    llm = build_llm(settings.llm) if need_llm else None
    db = build_db(settings.db, registry) if need_db else None

    embedder = None
    doc_retriever = None
    if need_kb and getattr(settings.kb, "enabled", True):
        try:
            embedder = build_embedder(settings.embedding, settings.llm)
            doc_retriever = build_doc_retriever(settings, embedder, llm)
        except Exception as e:  # noqa: BLE001 - 知识库不可用不应阻塞问数主流程
            _log.warning("知识库初始化失败，降级为纯 SQL 问数: %s", e)

    return AppContext(
        settings=settings,
        registry=registry,
        store=store,
        layer=layer,
        llm=llm,
        db=db,
        embedder=embedder,
        doc_retriever=doc_retriever,
    )


def make_engine_factory(
    ctx: AppContext,
    *,
    orchestrator: str = "pipeline",
) -> Callable[[Principal, DataPolicy], Any]:
    """返回 `factory(principal, policy) -> engine`。

    orchestrator = "pipeline"（手写编排）| "graph"（LangGraph 编排）；
    两者共用同一批组件与同一套权限守卫，只换编排方式——便于对照与演示。
    """
    settings = ctx.settings

    # 第二种执行引擎（ES）：backend 无状态、线程安全，整个 factory 只建一次复用。
    es_backend = build_es_backend(settings)
    es_engine = None
    if es_backend is not None:
        from examples.es_engine import EsQueryEngine

        es_engine = EsQueryEngine(es_backend, index=settings.es.index)
        _log.info("已启用第二执行引擎 ES: index=%s", settings.es.index)

    def factory(principal: Principal, policy: DataPolicy):
        guard = PolicyGuard(policy)  # 每会话独立：applied 不跨会话累积
        _log.info(
            "构建引擎: sub=%s role=%s orchestrator=%s data_scope=%s",
            principal.sub, principal.role.value, orchestrator, policy.describe(),
        )
        pipeline = _make_pipeline(ctx, guard)
        engine = _make_engine(ctx, pipeline, guard)
        if orchestrator == "graph":
            # 图编排：GraphRunner 暴露与 pipeline 相同的 query() 契约，
            # 引擎逻辑一行不改即完成编排替换（见 graph.GraphRunner 的说明）。
            engine.runner = _make_graph_runner(ctx, guard)
        if es_engine is not None:
            # 事件流水问题走 ES；行级权限从同一 policy 映射，安全等级不降级。
            from examples.es_engine import HybridRouter, scope_filters_from_policy

            scope = scope_filters_from_policy(policy)
            return HybridRouter(engine, es_engine, scope_filters=scope)
        return engine

    return factory


def _make_graph_runner(ctx: AppContext, guard: PolicyGuard):
    from .graph import GraphRunner, Text2SQLGraph

    settings = ctx.settings
    graph = Text2SQLGraph(
        registry=ctx.registry,
        store=ctx.store,
        llm=ctx.llm,
        db=ctx.db,
        retriever=_build_store_retriever(ctx),
        graph=getattr(ctx.layer, "graph", None),   # 业务知识图谱 -> Schema Linking
        top_k=settings.retrieval.top_k,
        min_score=settings.retrieval.min_score,
        max_retry=settings.pipeline.max_retry,
        critique_llm=bool(getattr(settings.pipeline, "critique_llm", False)),
        guard=guard,
    )
    return GraphRunner(graph)


def _build_store_retriever(ctx: AppContext):
    """示例库检索器：有向量索引时用「标签 ⊕ 向量 → RRF」，否则退回纯标签。"""
    try:
        from .retrieval import build_retriever

        if ctx.embedder is None:
            raise RuntimeError("embedder 未初始化")
        return build_retriever(ctx.settings, ctx.store, ctx.embedder)
    except Exception as e:  # noqa: BLE001
        _log.warning("示例库向量检索不可用，退回标签检索: %s", e)
        return None


def _make_pipeline(ctx: AppContext, guard: PolicyGuard) -> Text2SQLPipeline:
    settings = ctx.settings
    pipeline = Text2SQLPipeline(
        registry=ctx.registry,
        store=ctx.store,
        llm=ctx.llm,
        db=ctx.db,
        retriever=_build_store_retriever(ctx),
        graph=getattr(ctx.layer, "graph", None),   # 业务知识图谱 -> Schema Linking
        top_k=settings.retrieval.top_k,
        min_score=settings.retrieval.min_score,
        max_retry=settings.pipeline.max_retry,
        safety=SafetyGuard(),   # 生产入口统一开启输入护栏（越界/注入/密钥/PII）
    )
    pipeline.guard = guard
    return pipeline


def _make_engine(ctx: AppContext, pipeline: Text2SQLPipeline, guard: PolicyGuard):
    settings = ctx.settings
    from examples.grg_engine import GRGQueryEngine

    return GRGQueryEngine(
        pipeline,
        ctx.layer,
        doc_retriever=ctx.doc_retriever,
        doc_max_chars=settings.kb.doc_max_chars,
        guard=guard,
    )


def build_service(settings: Optional[Settings] = None, *, orchestrator: str = "pipeline", **kw):
    """一步到位：上下文 -> 服务（api.py / mcp_server.py 都从这里拿）。"""
    from .service import QueryService

    ctx = build_app_context(settings, **kw)
    return QueryService(
        make_engine_factory(ctx, orchestrator=orchestrator),
        settings=ctx.settings,
        registry=ctx.registry,
        layer=ctx.layer,
        db=ctx.db,
        doc_retriever=ctx.doc_retriever,
    ), ctx
