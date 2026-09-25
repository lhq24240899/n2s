"""数据模型层：贯穿检索、链接、生成、校验、回退全流程的纯数据结构。

所有模块之间只通过这些 dataclass 通信，互不耦合，方便单测与替换实现。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ResultSource(str, Enum):
    """最终 SQL 的来源，用于定位故障发生在哪一层。"""

    LLM = "llm"                       # LLM 正常生成且通过校验 + 预检
    FALLBACK_GENERIC = "fallback_generic"   # 检索无命中，走通用提示词兜底
    FALLBACK_TEMPLATE = "fallback_template" # 重试耗尽，回退到最相关示例 SQL
    CACHED = "cached"                 # 命中缓存（扩展位，暂未实现）


@dataclass
class TableSchema:
    """单张表的元信息，是 Schema Linking 与校验的权威数据源。"""

    name: str
    columns: dict[str, str]                          # 列名 -> 类型
    description: str = ""                            # 中文业务含义，用于匹配问题
    foreign_keys: list[tuple[str, str, str]] = field(default_factory=list)
    # (本表列, 引用表, 引用列)
    sample_values: dict[str, list] = field(default_factory=dict)
    # 可选：给 LLM 看的示例值，显著降低枚举类字段的幻觉


@dataclass
class SQLExample:
    """一条带标签的 (问题, SQL) 范例，构成可解释检索库的核心。"""

    id: str
    question: str
    sql: str
    domain: list[str] = field(default_factory=list)       # 业务域：["订单","支付"]
    intent: list[str] = field(default_factory=list)       # 意图：["聚合","TopN"]
    tables: list[str] = field(default_factory=list)       # 涉及表：["orders"]
    metrics: list[str] = field(default_factory=list)      # 指标：["GMV"]
    dimensions: list[str] = field(default_factory=list)   # 维度：["日期","地区"]
    keywords: list[str] = field(default_factory=list)     # 其他匹配词


@dataclass
class RetrievalHit:
    """一次检索命中，reasons 字段保证「为什么命中」完全可解释。"""

    example: SQLExample
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class AttemptTrace:
    """记录单次生成的完整轨迹，便于复现与排错。"""

    attempt_no: int
    prompt: str
    raw: str
    sql: str
    validation_error: Optional[str] = None
    execution_error: Optional[str] = None
    ok: bool = False


@dataclass
class PipelineTrace:
    """整条链路的诊断快照：哪一层命中、哪一层失败，一眼可见。"""

    question: str
    retrieval_hits: list[RetrievalHit] = field(default_factory=list)
    candidate_tables: list[str] = field(default_factory=list)
    allowed_tables: list[str] = field(default_factory=list)
    attempts: list[AttemptTrace] = field(default_factory=list)
    final_source: Optional[str] = None
    final_sql: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "retrieval_hits": [
                {
                    "id": h.example.id,
                    "score": round(h.score, 2),
                    "reasons": h.reasons,
                }
                for h in self.retrieval_hits
            ],
            "candidate_tables": self.candidate_tables,
            "allowed_tables": self.allowed_tables,
            "attempts": [
                {
                    "attempt_no": a.attempt_no,
                    "sql": a.sql,
                    "validation_error": a.validation_error,
                    "execution_error": a.execution_error,
                    "ok": a.ok,
                }
                for a in self.attempts
            ],
            "final_source": self.final_source,
            "final_sql": self.final_sql,
            "error": self.error,
        }


@dataclass
class GenerationResult:
    """管道最终产出：SQL + 来源标记 + 错误 + 全链路 trace。"""

    sql: Optional[str]
    raw: str
    source: ResultSource = ResultSource.LLM
    error: Optional[str] = None
    trace: Optional[PipelineTrace] = None
