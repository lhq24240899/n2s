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
from .safety import SafetyGuard
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
        graph=None,
        top_k: int = 5,
        min_score: float = 1.0,
        max_retry: int = 1,
        retry_on_empty: bool = True,
        safety: Optional[SafetyGuard] = None,
    ):
        self.registry = registry
        self.store = store
        self.llm = llm
        self.db = db
        self.glossary = glossary
        self.validator = validator or SQLValidator(registry, dialect=registry.dialect)
        self.prompt = prompt_builder or PromptBuilder(registry)
        # 业务知识图谱（SemanticLayer.graph）：让 Schema Linking 能把中文业务词
        # 映射到英文物理表——否则「各实验室的设备利用率」一张表都抽不到，
        # 只能靠检索命中兜着走（实测过，见 linker.py 的注释）
        self.linker = linker or SchemaLinker(registry, graph=graph)
        self.retriever = retriever or RetrievalService(store, min_score=min_score)
        self.top_k = top_k
        self.max_retry = max_retry
        # 空结果自愈：SQL 能执行但匹配 0 行 / 全 NULL 时，带反馈重生成一次
        # （这类失败静态校验与 EXPLAIN 都发现不了，只有执行后才知道）
        self.retry_on_empty = retry_on_empty
        # 输入安全护栏（safety.SafetyGuard）：链路最前端的越界/注入/密钥拦截。
        # 默认 None（不拦截），由装配层（bootstrap）或测试显式开启；这样不破坏
        # 既有「无护栏」的离线测试，又能在生产入口统一开启。
        self.safety = safety
        # 企业知识库摘录（混合检索结果），由上层每轮注入；用于补充业务口径与已知坑
        self.doc_context: Optional[str] = None
        # 数据权限守卫（policy.PolicyGuard），由上层每轮注入。
        # 未注入时不改变任何行为；注入后：生成前收窄可见表、生成后做列级检查 + 行级过滤注入。
        self.guard = None
        self._log = logging.getLogger("nl2sql.pipeline")

    # ---------------- 对外 API ----------------

    def run(self, question: str, pre_feedback: Optional[str] = None) -> GenerationResult:
        trace = PipelineTrace(question=question)

        # 0) 输入安全护栏（最前端拦截）：越界 / 提示词注入 / 密钥提取 / PII。
        #    命中即返回 REFUSED，绝不进入检索/LLM/DB，成本最低、最干净。
        if self.safety is not None:
            ref = self.safety.screen(question)
            if ref is not None:
                self._log.info(
                    "输入护栏拦截: category=%s | %s", ref.category.value, ref.reason
                )
                return self._finish(
                    ResultSource.REFUSED, None, "", trace, ref.reason
                )

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
        if self.guard is not None:
            # 数据权限：无权表直接不出现在 prompt 里（LLM 看不到，自然写不出）
            schemas = self.guard.constrain_schemas(schemas)
            if not schemas:
                raise PermissionError("你的数据权限不允许查询该问题涉及的表")
        allowed = [t.name for t in schemas]
        trace.candidate_tables = candidate
        trace.allowed_tables = allowed
        self._log.info("候选表: %s", allowed)
        # Schema Linking 的可解释依据（字面命中/检索继承/知识图谱/兜底各贡献了什么）
        for r in getattr(self.linker, "last_reasons", []):
            self._log.info("  └ linking 依据: %s", r)

        # 3) 生成 + 校验 + 预检 + 重试
        error_feedback: Optional[str] = pre_feedback
        last_raw = ""
        for attempt in range(self.max_retry + 1):
            prompt = self.prompt.build(
                question, schemas, hits, self.glossary, error_feedback, self.doc_context
            )
            raw = self.llm.generate(prompt, SYSTEM_PROMPT)
            last_raw = raw
            sql = self.extract_sql(raw)
            at = AttemptTrace(attempt_no=attempt + 1, prompt=prompt, raw=raw, sql=sql)

            err = self.validator.validate(sql, allowed)
            if err is None and self.guard is not None:
                # 数据权限：列级检查 + 行级过滤注入。改写后的 SQL 才是最终执行的 SQL，
                # 因此要写回 at.sql / trace，保证"看到的 SQL"就是"真正执行的 SQL"。
                sql, err = self.guard.post_sql(sql)
                at.sql = sql
                if err:
                    # ⚠️ 列级权限拒绝时必须**清空对外 SQL**（只留在 attempt trace 里供审计）。
                    # 此前只有回退路径做了 `sql = None`，主路径会把引用了敏感字段的 SQL
                    # 一路带进结果 —— 网页的「生成的 SQL」和 API 响应都会回显它。
                    # 那意味着"被拒的 SQL 不回显"这条不变量**只是靠回退路径偶然成立**：
                    # 离线测试 test_graph_blocks_unauthorized_column 一直绿，直到语义层的
                    # 示例 SQL 改写后才暴露（见本轮口径审计）。
                    at.validation_error = err
                    sql = None
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
                question, full_schemas, [], self.glossary, error_feedback, self.doc_context
            )
            raw = self.llm.generate(prompt, SYSTEM_PROMPT)
            last_raw = raw
            sql = self.extract_sql(raw)
            err = self.validator.validate(sql, self.registry.names())
            # 回退路径同样必须过数据权限，否则"重试耗尽"就成了绕过权限的后门
            if err is None and self.guard is not None:
                sql, err = self.guard.post_sql(sql)
                if err:
                    sql = None  # 宁可不返回数据，也不返回越权 SQL
            return self._finish(
                ResultSource.FALLBACK_GENERIC, sql, raw, trace, err
            )

        # 4b) 模板回退：返回最相关示例的 SQL（已被我们的口径验证过）
        self._log.info("重试耗尽，回退到最相关示例 SQL")
        sql = hits[0].example.sql
        guard_err = self.validator.validate(sql, allowed)  # 复核模板（信任但校验）
        if guard_err is None and self.guard is not None:
            sql, guard_err = self.guard.post_sql(sql)
            if guard_err:
                self._log.warning("回退模板未通过数据权限: %s", guard_err)
                sql = None
        return self._finish(
            ResultSource.FALLBACK_TEMPLATE, sql, last_raw, trace, guard_err or error_feedback
        )

    # 空结果自愈时回灌给 LLM 的反馈
    EMPTY_FEEDBACK = (
        "上一次生成的 SQL 语法正确、也通过了数据库预检，但执行后**返回 0 行或全为 NULL**。"
        "这通常说明过滤条件的取值与库内真实数据不一致，例如："
        "区域被写成了用户口语（如 '华南区'）而库里实际是 '华南'；"
        "业务线用了中文名而库里是英文 code；状态/口径字段取值写错。"
        "请严格使用『可用表结构』里『取值约束』给出的值，"
        "并且不要为问题中未提及的维度添加过滤条件。"
    )

    def query(
        self,
        question: str,
        pre_feedback: Optional[str] = None,
    ) -> tuple[GenerationResult, list[str], list[tuple]]:
        """run + 真实执行，返回 (结果, 列名, 行数据)，便于端到端演示。

        额外做一层「空结果自愈」：SQL 能跑通但匹配 0 行 / 全 NULL 时，
        带反馈重新生成一次——这是静态校验与 EXPLAIN 都覆盖不到的失败类型。
        """
        res = self.run(question, pre_feedback=pre_feedback)
        cols, rows = self._execute(res)

        if (
            self.retry_on_empty
            and res.sql
            and res.source is ResultSource.LLM
            and self._is_empty(cols, rows)
        ):
            self._log.warning("结果为空或全 NULL，触发空结果自愈（带反馈重生成）")
            res2 = self.run(question, pre_feedback=self.EMPTY_FEEDBACK)
            cols2, rows2 = self._execute(res2)
            return res2, cols2, rows2

        return res, cols, rows

    def _execute(self, res: GenerationResult) -> tuple[list[str], list[tuple]]:
        cols: list[str] = []
        rows: list[tuple] = []
        # 关键：只有「通过校验、无错误」的 SQL 才允许下发执行。
        # 否则回退路径若返回了一条未通过校验的 SQL（如被注入诱导吐出的 DROP），
        # 这里会直接把它丢给数据库——这是对抗性场景下的真实隐患，必须挡住。
        if res.sql and res.error is None:
            try:
                cols, rows = self.db.execute(res.sql)
            except Exception as e:  # noqa: BLE001
                res.error = f"执行失败: {e}"
        return cols, rows

    @staticmethod
    def _is_empty(cols: list[str], rows: list[tuple]) -> bool:
        """空结果判定：无行，或所有单元格均为 NULL。"""
        if not rows:
            return True
        return all(v is None for r in rows for v in r)

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
