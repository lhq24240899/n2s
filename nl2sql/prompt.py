"""Prompt 构建：把「问题 + 已链接 schema + 口径 + 参考示例 + 错误反馈」拼成给 LLM 的提示词。

关键约束（写在 system 里，且每次都强调）：
- 只读 SELECT，禁止任何写操作。
- 只输出 SQL，不带 markdown 代码块、不带解释。
- 参考示例「仅供风格与口径参考」，禁止照抄表名/列名（避免示例带偏）。
"""
from __future__ import annotations

from typing import Optional

from .glossary import Glossary
from .knowledge import SchemaRegistry
from .models import RetrievalHit, TableSchema

SYSTEM_PROMPT = (
    "你是一个资深数据分析师，负责把自然语言转为 SQL。"
    "规则：1) 只能生成只读 SELECT（或 WITH ... SELECT）查询；"
    "2) 严禁 INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/CREATE/GRANT；"
    "3) 只输出一条 SQL，不要解释，不要 markdown 代码块，不要分号结尾以外的多余字符；"
    "4) 必须且只能使用下方『可用表结构』中给出的表和列，禁止臆造。"
)


class PromptBuilder:
    def __init__(self, registry: SchemaRegistry):
        self.registry = registry

    def build(
        self,
        question: str,
        schemas: list[TableSchema],
        hits: list[RetrievalHit],
        glossary: Optional[Glossary] = None,
        error_feedback: Optional[str] = None,
        doc_context: Optional[str] = None,
    ) -> str:
        parts: list[str] = [f"# 任务\n{question}\n"]

        parts.append("# 可用表结构（已做 Schema Linking，仅含相关表）")
        for t in schemas:
            cols = ", ".join(f"{c} {ty}" for c, ty in t.columns.items())
            parts.append(f"- {t.name}({cols})  -- {t.description}")
            for col, ref_t, ref_c in t.foreign_keys:
                parts.append(f"  外键: {t.name}.{col} -> {ref_t}.{ref_c}")
            # 枚举类字段的可选值：显著降低 LLM 把口语原话（如"华南区"）写进 WHERE 的幻觉
            for col, values in (t.sample_values or {}).items():
                opts = ", ".join(f"'{v}'" for v in values)
                parts.append(f"  取值约束: {t.name}.{col} 仅能取 {opts}")

        # 企业知识库摘录（混合检索结果）：补充表结构看不出来的业务口径与已知坑
        if doc_context:
            parts.append(doc_context)

        if glossary:
            rendered = glossary.render()
            if rendered:
                parts.append(rendered)

        if hits:
            parts.append("# 参考示例（仅供风格与口径参考，不要照抄表名/列名）")
            for h in hits:
                parts.append(f"问题: {h.example.question}\nSQL: {h.example.sql}")

        if error_feedback:
            parts.append("# 上次生成失败，请根据以下错误修复")
            parts.append(error_feedback)

        parts.append("# 输出\n只输出一条 SQL。")
        return "\n".join(parts)
