"""主流程编排：把六层串成一条可观测、可回退的链路。

链路顺序：
  检索(retrieval) -> Schema Linking -> 构建 Prompt -> LLM 生成
  -> 静态校验(validator) -> 执行预检(dry_run) -> 重试(携带错误反馈)
  -> 失败回退(fallback_generic / fallback_template)

每一层的结果都写入 PipelineTrace，最终通过 GenerationResult.source 区分来源，
通过 trace 精确定位「是哪一层出的问题」——这是排错与对老板汇报的关键。
"""
from __future__ import annotations

import logging
import re
from typing import Optional

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
from .prompt import SYSTEM_PROMPT, PromptBuilder
from .retrieval import RetrievalService
from .validation import SQLValidator


class Text2SQLPipeline:
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
        self._log = logging.getLogger("nl2sql.pipeline")

    # ---------------- 对外 API ----------------

    def run(self, question: str) -> GenerationResult:
        trace = PipelineTrace(question=question)

        # 1) 检索
        hits = self.retriever.retrieve(question, top_k=self.top_k)
        trace.retrieval_hits = hits
        self._log_retrieval(question, hits)

        # 2) Schema Linking
        candidate = self.linker.infer_tables(question, hits)
        if not candidate:
            # 兜底：给全量 schema，让 LLM 自己挑（生产可改为让用户澄清）
            candidate = self.registry.names()
            self._log.warning("未抽到候选表，使用全量 schema 进行通用尝试")
        schemas = self.registry.link(candidate)
        allowed = [t.name for t in schemas]
        trace.candidate_tables = candidate
        trace.allowed_tables = allowed
        self._log.info("候选表: %s", allowed)

        # 3) 生成 + 校验 + 预检 + 重试
        error_feedback: Optional[str] = None
        last_raw = ""
        for attempt in range(self.max_retry + 1):
            prompt = self.prompt.build(
                question, schemas, hits, self.glossary, error_feedback
            )
            raw = self.llm.generate(prompt, SYSTEM_PROMPT)
            last_raw = raw
            sql = self.extract_sql(raw)
            at = AttemptTrace(attempt_no=attempt + 1, prompt=prompt, raw=raw, sql=sql)

            err = self.validator.validate(sql, allowed)
            if err is None:
                ok, exec_err = self.db.explain(sql)
                if ok:
                    at.ok = True
                    trace.attempts.append(at)
                    return self._finish(ResultSource.LLM, sql, raw, trace, None)
                err = f"执行预检失败: {exec_err}"
                at.execution_error = exec_err
            else:
                at.validation_error = err

            at.ok = False
            trace.attempts.append(at)
            self._log.warning("第 %d 次生成失败: %s", attempt + 1, err)
            error_feedback = err

        # 4) 重试耗尽 -> 回退
        if not hits:
            # 4a) 通用回退：检索无命中，用全量 schema 再生成一次
            self._log.info("无检索命中，走通用提示词回退")
            full_schemas = self.registry.link(self.registry.names())
            prompt = self.prompt.build(
                question, full_schemas, [], self.glossary, error_feedback
            )
            raw = self.llm.generate(prompt, SYSTEM_PROMPT)
            last_raw = raw
            sql = self.extract_sql(raw)
            err = self.validator.validate(sql, self.registry.names())
            return self._finish(
                ResultSource.FALLBACK_GENERIC, sql, raw, trace, err
            )

        # 4b) 模板回退：返回最相关示例的 SQL（已被我们的口径验证过）
        self._log.info("重试耗尽，回退到最相关示例 SQL")
        sql = hits[0].example.sql
        self.validator.validate(sql, allowed)  # 复核模板（信任但校验）
        return self._finish(
            ResultSource.FALLBACK_TEMPLATE, sql, last_raw, trace, error_feedback
        )

    def query(self, question: str) -> tuple[GenerationResult, list[str], list[tuple]]:
        """run + 真实执行，返回 (结果, 列名, 行数据)，便于端到端演示。"""
        res = self.run(question)
        cols: list[str] = []
        rows: list[tuple] = []
        if res.sql:
            try:
                cols, rows = self.db.execute(res.sql)
            except Exception as e:  # noqa: BLE001
                res.error = f"执行失败: {e}"
        return res, cols, rows

    # ---------------- 内部工具 ----------------

    @staticmethod
    def extract_sql(raw: str) -> str:
        raw = raw.strip()
        raw = re.sub(r"^```(?:sql)?\s*|\s*```$", "", raw, flags=re.MULTILINE | re.IGNORECASE).strip()
        m = re.search(r"(select|with)\b.*", raw, re.IGNORECASE | re.DOTALL)
        return m.group(0).strip() if m else raw

    @staticmethod
    def _finish(
        source: ResultSource,
        sql: Optional[str],
        raw: str,
        trace: PipelineTrace,
        error: Optional[str],
    ) -> GenerationResult:
        trace.final_source = source.value
        trace.final_sql = sql
        trace.error = error
        return GenerationResult(sql=sql, raw=raw, source=source, error=error, trace=trace)

    def _log_retrieval(self, q: str, hits: list[RetrievalHit]) -> None:
        self._log.info("检索问题: %s | 命中 %d 条", q, len(hits))
        for h in hits:
            self._log.info("  - %s score=%.2f reasons=%s", h.example.id, h.score, h.reasons)
