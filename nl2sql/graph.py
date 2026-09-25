"""LangGraph 编排：把六层链路显式建模成「有状态的图」，作为手写 pipeline 的**对照实现**。

为什么两套并存？
- 本实现**复用 pipeline 的全部组件类**（Retriever / Linker / PromptBuilder / LLM /
  Validator / DB），只把「编排」换掉。于是能精确讲清框架的边界：
  · 框架给的是：状态管理、条件路由、断点续跑（checkpoint）、人工介入（interrupt）、
    可视化（mermaid）、事件流；
  · 框架**不给**的是：口径治理、SQL 安全校验、检索可解释性——这些仍是自己的组件。
- "手写过 + 也用过框架"，才说得清框架到底解决了什么问题。

图结构（节点 = 步骤，边 = 流转条件）：

    START → retrieve → link → generate → validate ─ok→ execute → critique ─ok→ END
                       ▲          │err           │err               │revise
                       └──────────┴──────────────┘                  │
                          还有重试额度则带反馈重生成                   │
                          额度耗尽 → fallback → END ←────────────────┘

与 pipeline.py 的差异：pipeline 把重试/自愈/回退写成嵌套 for-if；
这里把「步骤」与「流转条件」分离，加节点或改路由都是局部改动，且天然可画图。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from .db import DBRunner
from .glossary import Glossary
from .knowledge import SchemaRegistry, SQLExampleStore
from .linker import SchemaLinker
from .llm import LLMClient
from .models import (
    AttemptTrace,
    GenerationResult,
    PipelineTrace,
    ResultSource,
    RetrievalHit,
)
from .pipeline import Text2SQLPipeline
from .prompt import SYSTEM_PROMPT, PromptBuilder
from .retrieval import RetrievalService
from .validation import SQLValidator

CRITIC_SYSTEM_PROMPT = (
    "你是 SQL 质量评审员。判断给定 SQL 是否真正回答了用户问题（口径、过滤条件、分组是否合理）。"
    "合理就只回复 OK；不合理回复 REVISE: 一句话原因。不要输出其它内容。"
)


class SQLGraphState(TypedDict, total=False):
    """图的状态：一次查询在各节点间传递的全部信息。"""

    question: str
    pre_feedback: Optional[str]
    doc_context: Optional[str]
    glossary: Optional[Glossary]

    hits: list[RetrievalHit]
    candidate_tables: list[str]
    allowed_tables: list[str]
    schemas: list[Any]

    prompt: str
    raw: str
    sql: Optional[str]
    error: Optional[str]
    attempt: int
    max_attempts: int

    cols: list[str]
    rows: list[tuple]

    verdict: str          # ok | revise | fail
    critique: str
    source: str
    steps: list[str]      # 人类可读的流程轨迹，便于演示与排错


class Text2SQLGraph:
    """LangGraph 版 NL2SQL 编排（组件与 Text2SQLPipeline 完全共用）。"""

    def __init__(
        self,
        registry: SchemaRegistry,
        store: SQLExampleStore,
        llm: LLMClient,
        db: DBRunner,
        glossary: Optional[Glossary] = None,
        *,
        validator: Optional[SQLValidator] = None,
        prompt_builder: Optional[PromptBuilder] = None,
        linker: Optional[SchemaLinker] = None,
        retriever: Optional[RetrievalService] = None,
        top_k: int = 5,
        min_score: float = 1.0,
        max_retry: int = 1,
        critique_llm: bool = False,
    ):
        self.registry = registry
        self.store = store
        self.llm = llm
        self.db = db
        self.glossary = glossary
        self.validator = validator or SQLValidator(registry, dialect=registry.dialect)
        self.prompt = prompt_builder or PromptBuilder(registry)
        self.linker = linker or SchemaLinker(registry)
        self.retriever = retriever or RetrievalService(store, min_score=min_score)
        self.top_k = top_k
        self.max_retry = max_retry
        # 是否额外调用 LLM 做"结果一致性复核"（默认关，避免每次查询都双倍成本）
        self.critique_llm = critique_llm
        self._log = logging.getLogger("nl2sql.graph")
        self.app = self._build()

    # ---------------- 构图 ----------------

    def _build(self):
        g = StateGraph(SQLGraphState)
        g.add_node("retrieve", self._n_retrieve)
        g.add_node("link", self._n_link)
        g.add_node("generate", self._n_generate)
        g.add_node("validate", self._n_validate)
        g.add_node("execute", self._n_execute)
        g.add_node("critique", self._n_critique)
        g.add_node("fallback", self._n_fallback)

        g.add_edge(START, "retrieve")
        g.add_edge("retrieve", "link")
        g.add_edge("link", "generate")
        g.add_edge("generate", "validate")
        g.add_conditional_edges(
            "validate",
            self._route_after_check,
            {"execute": "execute", "retry": "generate", "fallback": "fallback"},
        )
        g.add_conditional_edges(
            "execute",
            self._route_after_execute,
            {"critique": "critique", "retry": "generate", "fallback": "fallback"},
        )
        g.add_conditional_edges(
            "critique",
            self._route_after_critique,
            {"done": END, "retry": "generate", "fallback": "fallback"},
        )
        g.add_edge("fallback", END)
        return g.compile()

    # ---------------- 工具 ----------------

    def _merge(self, state: SQLGraphState, step: str, **updates) -> dict:
        merged: dict = dict(updates)
        merged["steps"] = list(state.get("steps") or []) + [step]
        return merged

    def _budget_left(self, state: SQLGraphState) -> bool:
        """是否还有重试额度。max_attempts = max_retry + 1（首次 + 重试次数）。"""
        return int(state.get("attempt") or 1) < int(state.get("max_attempts") or 1)

    # ---------------- 节点 ----------------

    def _n_retrieve(self, state: SQLGraphState) -> dict:
        hits = self.retriever.retrieve(state["question"], top_k=self.top_k)
        return self._merge(state, f"retrieve：命中 {len(hits)} 条参考示例", hits=hits)

    def _n_link(self, state: SQLGraphState) -> dict:
        candidate = self.linker.infer_tables(state["question"], state.get("hits") or [])
        if not candidate:
            candidate = self.registry.names()
        schemas = self.registry.link(candidate)
        return self._merge(
            state,
            f"link：候选表 {len(schemas)} 张 {[t.name for t in schemas]}",
            candidate_tables=candidate,
            allowed_tables=[t.name for t in schemas],
            schemas=schemas,
        )

    def _n_generate(self, state: SQLGraphState) -> dict:
        attempt = int(state.get("attempt") or 0) + 1
        feedback = state.get("error") or state.get("pre_feedback")
        prompt = self.prompt.build(
            state["question"],
            state.get("schemas") or [],
            state.get("hits") or [],
            state.get("glossary") or self.glossary,
            feedback,
            state.get("doc_context"),
        )
        raw = self.llm.generate(prompt, SYSTEM_PROMPT)
        sql = Text2SQLPipeline.extract_sql(raw)
        note = "（带错误反馈重生成）" if feedback else ""
        return self._merge(
            state,
            f"generate：第 {attempt} 次生成{note}",
            attempt=attempt,
            prompt=prompt,
            raw=raw,
            sql=sql,
            error=None,
        )

    def _n_validate(self, state: SQLGraphState) -> dict:
        err = self.validator.validate(state.get("sql") or "", state.get("allowed_tables") or [])
        return self._merge(
            state,
            "validate：通过" if err is None else f"validate：拦截（{err[:60]}）",
            error=err,
        )

    def _n_execute(self, state: SQLGraphState) -> dict:
        sql = state.get("sql") or ""
        if not sql:
            return self._merge(state, "execute：无 SQL 可执行", error="没有生成出可执行的 SQL")
        ok, exec_err = self.db.explain(sql)
        if not ok:
            return self._merge(state, f"execute：预检失败（{exec_err}）", error=f"执行预检失败: {exec_err}")
        try:
            cols, rows = self.db.execute(sql)
        except Exception as e:  # noqa: BLE001
            return self._merge(state, f"execute：执行异常（{e}）", error=f"执行失败: {e}")
        return self._merge(state, f"execute：返回 {len(rows)} 行", cols=cols, rows=rows, error=None)

    def _n_critique(self, state: SQLGraphState) -> dict:
        """Critic 反思节点：先做零成本规则检查，再按需做 LLM 一致性复核。"""
        rows = state.get("rows") or []
        empty = (not rows) or all(v is None for r in rows for v in r)
        if empty:
            return self._merge(
                state,
                "critique：结果为空 → 要求修正",
                verdict="revise",
                critique="结果为空或全为 NULL",
                error=(
                    "上次 SQL 执行后返回 0 行或全 NULL。请检查过滤条件取值是否与库内一致"
                    "（如区域不带'区'字、业务线用英文 code），并去掉问题中未提及的过滤条件。"
                ),
            )

        if self.critique_llm:
            verdict, reason = self._llm_critique(state)
            if verdict == "revise":
                return self._merge(
                    state,
                    f"critique：LLM 复核未通过（{reason[:40]}）",
                    verdict="revise",
                    critique=reason,
                    error=f"评审未通过：{reason}。请据此修正 SQL。",
                )
            return self._merge(state, "critique：LLM 复核通过", verdict="ok", critique="LLM 复核通过")

        return self._merge(state, "critique：规则检查通过", verdict="ok", critique="规则检查通过")

    def _n_fallback(self, state: SQLGraphState) -> dict:
        hits = state.get("hits") or []
        glossary = state.get("glossary") or self.glossary

        if not hits:
            schemas = self.registry.link(self.registry.names())
            prompt = self.prompt.build(
                state["question"], schemas, [], glossary, state.get("error"), state.get("doc_context")
            )
            raw = self.llm.generate(prompt, SYSTEM_PROMPT)
            sql = Text2SQLPipeline.extract_sql(raw)
            source = ResultSource.FALLBACK_GENERIC.value
        else:
            sql = hits[0].example.sql
            raw = state.get("raw") or ""
            source = ResultSource.FALLBACK_TEMPLATE.value

        cols: list[str] = []
        rows: list[tuple] = []
        try:
            if sql:
                cols, rows = self.db.execute(sql)
        except Exception as e:  # noqa: BLE001
            return self._merge(
                state,
                f"fallback：{source} 执行失败（{e}）",
                sql=sql, raw=raw, source=source, error=f"执行失败: {e}",
            )
        return self._merge(
            state, f"fallback：{source}，返回 {len(rows)} 行", sql=sql, raw=raw, source=source
        )

    def _llm_critique(self, state: SQLGraphState) -> tuple[str, str]:
        sample = (state.get("rows") or [])[:5]
        prompt = (
            f"# 用户问题\n{state['question']}\n\n"
            f"# 生成的 SQL\n{state.get('sql')}\n\n"
            f"# 执行结果\n列：{state.get('cols')}\n前 5 行：{sample}\n\n"
            "# 任务\n判断上面 SQL 是否真正回答了用户问题。合理只回复 OK；"
            "不合理回复 REVISE: 一句话原因。"
        )
        try:
            out = (self.llm.generate(prompt, CRITIC_SYSTEM_PROMPT) or "").strip()
        except Exception:  # noqa: BLE001
            return "ok", ""  # 评审服务不可用时不应阻塞主流程
        if out.upper().startswith("OK"):
            return "ok", ""
        m = re.search(r"REVISE\s*[:：]?\s*(.+)", out, re.IGNORECASE | re.DOTALL)
        if m:
            return "revise", m.group(1).strip()[:200]
        return "ok", ""

    # ---------------- 路由 ----------------

    def _route_after_check(self, state: SQLGraphState) -> str:
        if not state.get("error"):
            return "execute"
        return "retry" if self._budget_left(state) else "fallback"

    def _route_after_execute(self, state: SQLGraphState) -> str:
        if not state.get("error"):
            return "critique"
        return "retry" if self._budget_left(state) else "fallback"

    def _route_after_critique(self, state: SQLGraphState) -> str:
        verdict = state.get("verdict")
        if verdict == "ok":
            return "done"
        if verdict == "revise":
            return "retry" if self._budget_left(state) else "fallback"
        return "fallback"

    # ---------------- 对外 ----------------

    def run(
        self,
        question: str,
        pre_feedback: Optional[str] = None,
        doc_context: Optional[str] = None,
        glossary: Optional[Glossary] = None,
    ) -> GenerationResult:
        """跑图，返回与 Text2SQLPipeline 完全一致的 GenerationResult（便于上层复用）。"""
        init: SQLGraphState = {
            "question": question,
            "pre_feedback": pre_feedback,
            "doc_context": doc_context,
            "glossary": glossary or self.glossary,
            "attempt": 0,
            "max_attempts": self.max_retry + 1,
            "steps": [],
            "verdict": "",
            "critique": "",
        }
        final: SQLGraphState = self.app.invoke(init)

        sql = final.get("sql")
        source = final.get("source") or ResultSource.LLM.value
        if final.get("error") and final.get("verdict") != "ok":
            self._log.warning("图执行结束但仍有未解决的错误: %s", final.get("error"))

        trace = PipelineTrace(question=question)
        trace.retrieval_hits = final.get("hits") or []
        trace.candidate_tables = final.get("candidate_tables") or []
        trace.allowed_tables = final.get("allowed_tables") or []
        trace.attempts = [
            AttemptTrace(
                attempt_no=int(final.get("attempt") or 1),
                prompt=final.get("prompt") or "",
                raw=final.get("raw") or "",
                sql=sql or "",
                ok=final.get("verdict") == "ok",
                validation_error=final.get("error"),
            )
        ]
        trace.final_source = source
        trace.final_sql = sql
        trace.error = final.get("error")

        res = GenerationResult(
            sql=sql,
            raw=final.get("raw") or "",
            source=ResultSource(source),
            error=final.get("error"),
            trace=trace,
        )
        # 附带图流程轨迹与执行结果，便于 UI/演示直接使用
        res.graph_steps = final.get("steps") or []          # type: ignore[attr-defined]
        res.cols = final.get("cols") or []                  # type: ignore[attr-defined]
        res.rows = final.get("rows") or []                  # type: ignore[attr-defined]
        res.critique = final.get("critique") or ""          # type: ignore[attr-defined]
        return res

    def mermaid(self) -> str:
        """导出 mermaid 图（LangGraph 自带能力：`print(app.get_graph().draw_mermaid())`）。"""
        return self.app.get_graph().draw_mermaid()


def build_graph(settings, registry, store, llm, db, glossary=None, retriever=None, embedder=None):
    """工厂：按 Settings 构建 LangGraph 版编排（与 pipeline 共用同一批组件）。"""
    from .kb import build_doc_retriever  # noqa: F401  仅保持与 pipeline 一致的扩展点

    return Text2SQLGraph(
        registry=registry,
        store=store,
        llm=llm,
        db=db,
        glossary=glossary,
        retriever=retriever,
        top_k=settings.retrieval.top_k,
        min_score=settings.retrieval.min_score,
        max_retry=settings.pipeline.max_retry,
    )
