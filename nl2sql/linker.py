"""Schema Linking：从「问题 + 检索命中」中抽取候选表，控制喂给 LLM 的 schema 规模。

为什么需要它：
- 真实数仓有几百张表，全量塞进 prompt 既超 token 又诱导幻觉。
- 这里用「问题字面匹配 + 检索命中继承 + 1 跳外键」三步，给出最小相关 schema。
"""
from __future__ import annotations

from typing import Optional

from .knowledge import SchemaRegistry
from .models import RetrievalHit


class SchemaLinker:
    def __init__(
        self,
        registry: SchemaRegistry,
        fallback_tables: Optional[list[str]] = None,
    ):
        self.registry = registry
        self.fallback_tables = fallback_tables or []

    def infer_tables(self, question: str, hits: list[RetrievalHit]) -> list[str]:
        names: set[str] = set()

        # 1) 问题中出现表名 / 表的中文描述 -> 直接命中
        for tname, t in self.registry.tables.items():
            if tname in question:
                names.add(tname)
            if t.description and t.description in question:
                names.add(tname)

        # 2) 继承检索命中的示例所涉及的表
        for h in hits:
            for t in h.example.tables:
                if t in self.registry.tables:
                    names.add(t)

        # 3) 兜底：什么都没抽到时，给一个最小核心表集合
        #    （生产可改为「要求用户澄清」，避免盲猜）
        if not names and self.fallback_tables:
            names = set(self.fallback_tables)

        return list(names)
