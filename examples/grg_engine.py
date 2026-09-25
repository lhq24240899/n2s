"""计量检测问数引擎编排：把通用 pipeline + 语义层 + 多轮上下文组合成业务引擎。

对外只暴露一个 ask(question)，内部完成：
  语义映射 → 歧义澄清? →（用户确认后回到原问题）→ 多轮上下文继承
  → 组装口径 Glossary → pipeline.query(归一化问题) → 更新上下文 → 返回结果

澄清闭环（关键设计）：
  遇到歧义时若只是回一句"请澄清"就结束，用户回"是的"会被当成一个**全新问题**——
  既丢掉原问题里的指标，又会错误地继承上一轮的旧指标（实测就出现过
  问"那个做环境的实验室利用率怎么样"、确认后却答成了"检测服务收入"）。
  因此这里把「原问题 + 歧义同义词 + 消歧后的取值」暂存为**待澄清态**；
  用户确认后用规范词重写原问题再跑一遍，保证指标/维度不丢、上下文不被污染。

混合 RAG（结构化 ⊕ 文档）：
  每轮先对企业知识库做**三路混合召回**（关键词 / pg_trgm / pgvector → RRF 融合）：
  - 解析出结构化意图（有指标或要求分组）-> 走 SQL 问数，并把知识库摘录作为
    「业务口径参考」注入生成 prompt（补上表结构看不出来的口径与已知坑）；
  - 解析不出结构化意图（如"EMC 是什么""为什么华南区查不到数据"）->
    走**文档问答**，只依据检索到的资料作答并标注引用来源，避免 RAG 变成新的幻觉源。

LLM 与 DB 均为真实实现（由 build_llm / build_db 注入）：
- 真实 LLM 靠 prompt 里的 schema + 口径 Glossary + 知识库摘录 + 参考示例来生成 SQL。
- 真实 DB 由 PsycopgRunner 执行 EXPLAIN 预检与查询，返回真实数据。

语义层（semantic.py / grg_schema.py）承载计量检测行业知识，是这套系统
区别于通用 Text-to-SQL 的关键。
"""
from __future__ import annotations

from nl2sql.context import QueryContext
from nl2sql.kb import answer_with_docs, build_sql_doc_block
from nl2sql.pipeline import Text2SQLPipeline
from nl2sql.semantic import MappedQuery, SemanticLayer, SemanticMapper

# 视为"确认"的答复（去掉标点后精确匹配，避免把新问题误判成确认）
CONFIRM_WORDS = {
    "是", "是的", "是的呢", "对", "对的", "嗯", "嗯嗯", "没错", "确定", "确认",
    "可以", "好", "好的", "行", "没问题", "yes", "y", "ok", "okay", "sure",
}
_PUNCT = "。，、！？!?,.;；:：~～ \t　\"'“”‘’"


class GRGQueryEngine:
    """计量检测问数引擎：语义层 + 多轮上下文 + 混合 RAG + 通用 pipeline 的组合层。"""

    def __init__(
        self,
        pipeline: Text2SQLPipeline,
        layer: SemanticLayer,
        doc_retriever=None,
        doc_max_chars: int = 1200,
    ):
        self.pipeline = pipeline
        self.layer = layer
        self.mapper = SemanticMapper(layer)
        self.context = QueryContext()
        self.pending: dict | None = None  # 待澄清态（见模块 docstring）
        # 可选：企业知识库混合检索器（三路召回 + RRF）。不传则退化为纯 SQL 问数。
        self.doc_retriever = doc_retriever
        self.doc_max_chars = doc_max_chars

    # ---------------- 对外 API ----------------

    def ask(self, question: str) -> dict:
        # 上一轮在等澄清、且本轮是对澄清的回应 -> 回到原问题重跑
        if self.pending is not None:
            pending, self.pending = self.pending, None
            if self._is_clarification_reply(question):
                return self._resume(pending, question)
            # 否则视为新问题，正常往下走（不污染上下文）

        return self._answer(self.mapper.map(question))

    def reset_context(self) -> None:
        self.context.reset()
        self.pending = None

    # ---------------- 澄清代答 ----------------

    def _resume(self, pending: dict, reply: str) -> dict:
        """用消歧后的规范词重写原问题，并把答复里的补充信息一并带上。"""
        rewritten = pending["question"]
        for phrase, canonical in pending["rewrite"]:
            rewritten = rewritten.replace(phrase, canonical)
        if not self._is_confirmation(reply):
            rewritten = f"{rewritten}；补充信息：{reply}"

        mapped = self.mapper.map(rewritten)
        # 双保险：确认下来的实体直接补进去（即使重写后没解析出来）
        for key, value in pending["entities"].items():
            mapped.entities.setdefault(key, value)
        mapped.reasons.append(
            f"澄清确认: 已消歧（{pending['desc']}）并回到上一轮问题"
        )
        return self._answer(mapped)

    @staticmethod
    def _make_pending(mapped: MappedQuery) -> dict:
        """把「原问题 + 消歧重写规则 + 消歧后的实体」存成待澄清态。"""
        rewrite = [(s.phrase, s.canonical) for s in mapped.ambiguous_synonyms]
        entities = {
            s.target: (s.value or s.canonical)
            for s in mapped.ambiguous_synonyms
            if s.target and s.target != "metric"
        }
        desc = "、".join(f"{p} -> {c}" for p, c in rewrite) or "无"
        return {
            "question": mapped.original,
            "rewrite": rewrite,
            "entities": entities,
            "desc": desc,
        }

    @staticmethod
    def _is_confirmation(text: str) -> bool:
        cleaned = "".join(ch for ch in text.strip().lower() if ch not in _PUNCT)
        return cleaned in CONFIRM_WORDS

    def _is_clarification_reply(self, question: str) -> bool:
        """判断本轮输入是不是对上一轮澄清的回应。

        - 明确的确认（是/对/好的…）；或
        - 短句且自身解析不出指标（如"是可靠性实验室"这类补充说明）

        若用户直接抛出一个带指标的新问题，则视为换话题，按新问题处理。
        """
        if self._is_confirmation(question):
            return True
        if len(question.strip()) > 12:
            return False
        return self.mapper.map(question).metric is None

    # ---------------- 主流程 ----------------

    def _answer(self, mapped: MappedQuery) -> dict:
        # 歧义优先：要求澄清，不进入生成；同时记下待澄清态
        if mapped.clarification:
            self.pending = self._make_pending(mapped)
            return {
                "type": "clarification",
                "message": mapped.clarification,
                "mapped": mapped,
            }

        # 多轮上下文继承（追问"那华南区呢" -> 仅替换区域，业务线/指标/时间沿用）
        merged = self.context.inherit(mapped)

        # 企业知识库混合检索（三路召回 + RRF）。
        # 用「本轮问题」而不是继承后的文本，避免继承来的维度词把文档检索带偏。
        docs = self.doc_retriever.retrieve(mapped.normalized) if self.doc_retriever else []

        # 路由：解析不出结构化意图（无指标、也没要求分组）-> 走文档问答（RAG）
        structured = merged.metric is not None or "group_by" in merged.entities
        if not structured and docs:
            answer = answer_with_docs(
                self.pipeline.llm, mapped.normalized, docs, self.doc_max_chars
            )
            return {
                "type": "rag",
                "mapped": merged,
                "answer": answer,
                "docs": docs,
            }

        # 结构化问数：把知识库摘录作为**业务口径补充**注入生成 prompt
        glossary = self.layer.glossary_for(
            merged.metric.id if merged.metric else None, merged.entities
        )
        self.pipeline.glossary = glossary
        self.pipeline.doc_context = build_sql_doc_block(docs, self.doc_max_chars) or None

        res, cols, rows = self.pipeline.query(merged.normalized)

        # 更新上下文，供下一轮继承
        self.context.update_from(merged)

        return {
            "type": "result",
            "mapped": merged,
            "result": res,
            "cols": cols,
            "rows": rows,
            "glossary": glossary,
            "docs": docs,
        }
