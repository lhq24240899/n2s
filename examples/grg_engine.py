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

from typing import Optional

from nl2sql.context import QueryContext
from nl2sql.kb import answer_with_docs, build_sql_doc_block
from nl2sql.pipeline import Text2SQLPipeline
from nl2sql.models import ResultSource
from nl2sql.policy import PolicyViolation
from nl2sql.safety import SafetyGuard
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
        guard=None,
        runner=None,
        safety=None,
    ):
        self.pipeline = pipeline
        self.layer = layer
        self.mapper = SemanticMapper(layer)
        self.context = QueryContext()
        self.pending: dict | None = None  # 待澄清态（见模块 docstring）
        # 可选：企业知识库混合检索器（三路召回 + RRF）。不传则退化为纯 SQL 问数。
        self.doc_retriever = doc_retriever
        self.doc_max_chars = doc_max_chars
        # 可选：数据权限守卫（policy.PolicyGuard）。不传则不做数据权限约束（本地 demo 场景）。
        self.guard = guard
        # 编排方式可替换：pipeline（手写）或 GraphRunner（LangGraph）。
        # 两者暴露同样的四个属性 + query()，因此引擎逻辑完全不用改。
        self.runner = runner or pipeline
        # 可选：输入安全护栏（nl2sql.safety.SafetyGuard）。不传则不过滤输入
        # （仅用于内部测试 / 已被外层 QueryService 拦截的场景）。生产入口务必传入，
        # 否则密钥提取 / 提示词注入 / PII / 越界问题会绕过护栏直接进 RAG 或 LLM。
        self.safety = safety

    # ---------------- 对外 API ----------------

    def ask(self, question: str) -> dict:
        # 输入安全护栏：密钥 / 提示词注入 / PII / 越界 —— 入口即拦，
        # 不进语义映射、不进 RAG、不进 LLM。这是对抗"问 apikey 是多少"这类攻击的最后关口。
        # （FastAPI / MCP 路径在 QueryService.ask 已先拦一次；这里再拦，保证 Streamlit 直连也不漏）
        if self.safety is not None:
            # 第一步：硬拦截（注入 / 密钥 / PII）——任何输入都拦，含澄清回复里夹带的攻击。
            ref = self.safety.screen(question, hard_only=True)
            if ref is not None:
                return self._refuse(ref)
            # 澄清回复：已过硬拦截，不再判越界（避免"是的"被当成无关问题拒绝、
            # 打断多轮澄清闭环）；交给原澄清逻辑回到上一轮问题重跑。
            if self.pending is not None and self._is_clarification_reply(question):
                pending, self.pending = self.pending, None
                return self._resume(pending, question)
            # 全新问题：完整拦截（含越界软拦截）。
            ref = self.safety.screen(question, hard_only=False)
            if ref is not None:
                return self._refuse(ref)
        else:
            # 未配置护栏：保持原有澄清逻辑
            if self.pending is not None:
                pending, self.pending = self.pending, None
                if self._is_clarification_reply(question):
                    return self._resume(pending, question)
                # 否则视为新问题，正常往下走（不污染上下文）

        return self._answer(self.mapper.map(question))

    @staticmethod
    def _refuse(ref) -> dict:
        """把 SafetyGuard.Refusal 转成统一的 refused 响应。"""
        return {
            "type": "refused",
            "category": ref.category.value,
            "answer": ref.safe_reply,
            "reason": ref.reason,
        }

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

        # 数据权限①：指标级拒绝。放在继承之后，才能连"追问带出来的旧指标"一起管住。
        # 明确拒绝比"生成一条被拦的 SQL"体验好得多，也避免把无权口径泄露到提示里。
        if self.guard is not None:
            try:
                self.guard.check_metric(merged.metric)
            except PolicyViolation as e:
                return {
                    "type": "denied",
                    "message": e.reason,
                    "detail": e.detail,
                    "mapped": merged,
                }

        # 企业知识库混合检索（三路召回 + RRF）。
        # 用「本轮问题」而不是继承后的文本，避免继承来的维度词把文档检索带偏。
        docs = self.doc_retriever.retrieve(mapped.normalized) if self.doc_retriever else []

        # 路由：解析不出结构化意图（无指标、无计数/排名意图、也没要求分组）-> 走文档问答（RAG）
        # （"多少台设备"这类总量问句没有注册指标，必须靠 count 意图保住结构化路由——
        #   评估集实测：缺这条规则时「总量」类 8 条全部被错误路由进 RAG）
        structured = (
            merged.metric is not None
            or "group_by" in merged.entities
            or "count" in merged.entities
            or "topn" in merged.entities
        )
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
        runner = self.runner
        runner.glossary = glossary
        runner.doc_context = build_sql_doc_block(docs, self.doc_max_chars) or None
        # 数据权限②：把守卫交给编排层，由它负责"隐藏无权表 + 列级拦截 + 行级过滤注入"
        runner.guard = self.guard

        try:
            res, cols, rows = runner.query(merged.normalized)
        except (PolicyViolation, PermissionError) as e:
            return {
                "type": "denied",
                "message": getattr(e, "reason", None) or str(e),
                "detail": getattr(e, "detail", ""),
                "mapped": merged,
            }

        # 护栏可能挂在 pipeline 而不是引擎上（API/MCP 由 QueryService 兜、graph 编排等）。
        # 必须把「拒绝」如实透传成 refused，否则调用方只看到 sql=None 的"空结果"——
        # 用户看到的是"无数据返回"而不是"我不能回答这个"，拦截语义被吞掉；
        # 更糟的是装配里少一层护栏时，拦截会**悄悄变成静默的空结果**，无从发现。
        if res.source is ResultSource.REFUSED:
            # 这一层只拿得到"拒绝原因"，拿不到面向用户的措辞（它在 Refusal.safe_reply 里）。
            # 所以原地再判一次，得到与网页/API 完全一致的回复文案；判不出来就退回通用文案。
            ref = (self.safety or SafetyGuard()).screen(mapped.original)
            payload = self._refuse(ref) if ref is not None else {
                "type": "refused",
                "answer": "该问题不在我的回答范围内。",
                "reason": res.error,
            }
            return {**payload, "mapped": merged, "result": res}

        # 空结果回退：结果是空的、且本轮带入了"上一轮继承"的过滤维度时，
        # 忽略这些继承维度再查一次。只在**继承来的**维度上放宽，用户本轮明说的条件绝不动。
        # 真机场景（用户报的 bug）：先问「华东区…准时率」，再问「EMC 检测的准时率是多少」——
        # EMC 的报告全在北京（华北），被继承来的「区域=华东」一过滤就为空，
        # 页面显示"无匹配数据"，用户以为系统算错了。
        context_fallback: Optional[dict] = None
        if self._is_empty_result(rows) and merged.inherited_dimensions:
            relaxed = self.context.inherit(mapped, inherit_filters=False)
            if relaxed.entities != merged.entities:
                # ⚠️ 必须重建 Glossary：口径注入会把实体渲染成「必须使用 labs.region = '华东'」
                # 这类**硬约束**塞进提示词。不重建的话，即使问题文本已经放宽，
                # LLM 仍会照着上一轮的口径把区域过滤写回 SQL —— 重查照样为空，回退形同虚设
                # （真机实测：第一版就踩了这个，回退静默失效）。
                relaxed_glossary = self.layer.glossary_for(
                    relaxed.metric.id if relaxed.metric else None, relaxed.entities
                )
                runner.glossary = relaxed_glossary
                try:
                    res2, cols2, rows2 = runner.query(relaxed.normalized)
                except (PolicyViolation, PermissionError):
                    runner.glossary = glossary     # 权限问题照旧上抛，不被回退吞掉
                    raise
                except Exception:  # noqa: BLE001 - 回退失败就保留原结果，不掩盖真实错误
                    runner.glossary = glossary
                else:
                    if not self._is_empty_result(rows2):
                        context_fallback = {
                            "dropped": [d for d in merged.inherited_dimensions if d != "metric"],
                            "normalized": relaxed.normalized,
                        }
                        res, cols, rows, merged = res2, cols2, rows2, relaxed
                        glossary = relaxed_glossary      # 返回的口径说明也要与结果一致
                    else:
                        runner.glossary = glossary       # 放宽后仍为空：恢复原口径，保留原结果

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
            "context_fallback": context_fallback,
        }

    @staticmethod
    def _is_empty_result(rows) -> bool:
        """空结果 = 没有行，或所有格子都是 NULL（单值查询取不到时是 [(None,)]）。"""
        if not rows:
            return True
        return all(v is None for r in rows for v in r)
