"""广电计量问数引擎编排：把通用 pipeline + 语义层 + 多轮上下文组合成业务引擎。

对外只暴露一个 ask(question)，内部完成：
  语义映射 → 歧义澄清? → 多轮上下文继承 → 组装口径 Glossary
  → pipeline.run(归一化问题) → 更新上下文 → 返回结果/澄清

LLM 与 DB 均为真实实现（由 build_llm / build_db 注入）：
- 真实 LLM 靠 prompt 里的 schema + 口径 Glossary + 参考示例来生成 SQL，
  不再需要业务感知的确定性 Mock。
- 真实 DB 由 PsycopgRunner 执行 EXPLAIN 预检与查询，返回真实数据。

语义层（semantic.py / grg_schema.py）承载广电计量行业知识，是这套系统
区别于通用 Text-to-SQL 的关键。
"""
from __future__ import annotations

from nl2sql.context import QueryContext
from nl2sql.models import ResultSource
from nl2sql.pipeline import Text2SQLPipeline
from nl2sql.semantic import SemanticLayer, SemanticMapper


class GRGQueryEngine:
    """广电计量问数引擎：语义映射 + 多轮上下文 + 通用 pipeline 的组合层。"""

    def __init__(self, pipeline: Text2SQLPipeline, layer: SemanticLayer):
        self.pipeline = pipeline
        self.layer = layer
        self.mapper = SemanticMapper(layer)
        self.context = QueryContext()

    def ask(self, question: str) -> dict:
        mapped = self.mapper.map(question)

        # 歧义优先：直接要求澄清，不进入生成
        if mapped.clarification:
            return {
                "type": "clarification",
                "message": mapped.clarification,
                "mapped": mapped,
            }

        # 多轮上下文继承（追问"那华南区呢" -> 仅替换区域，业务线/指标/时间沿用）
        merged = self.context.inherit(mapped)

        # 组装口径 Glossary（只注入本轮解析到的指标，控制 prompt 体积）
        glossary = self.layer.glossary_for(merged.metric.id if merged.metric else None)
        self.pipeline.glossary = glossary

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
        }

    def reset_context(self) -> None:
        self.context.reset()
