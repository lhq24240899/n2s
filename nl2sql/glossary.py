"""业务口径层（Glossary）：把「GMV」「活跃用户」等术语的统一定义显式喂给 LLM。

为什么要单独一层？
- 同一指标不同团队口径常不一致（GMV 含不含退款？活跃怎么算？）。
- 把这些口径写进 prompt，比让 LLM 自己猜更可控、可审计。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Metric:
    name: str
    definition: str  # 统一口径，例如 "SUM(orders.total_amount) WHERE orders.status='paid'"


@dataclass
class GlossaryEntry:
    term: str
    meaning: str


class Glossary:
    def __init__(
        self,
        metrics: list[Metric] | None = None,
        entries: list[GlossaryEntry] | None = None,
    ):
        self.metrics: dict[str, Metric] = {m.name: m for m in (metrics or [])}
        self.entries: dict[str, GlossaryEntry] = {e.term: e for e in (entries or [])}

    def render(self) -> str:
        lines: list[str] = []
        if self.metrics:
            lines.append("# 指标口径（务必遵守）")
            for m in self.metrics.values():
                lines.append(f"- {m.name} = {m.definition}")
        if self.entries:
            lines.append("# 业务术语")
            for e in self.entries.values():
                lines.append(f"- {e.term}: {e.meaning}")
        return "\n".join(lines)
