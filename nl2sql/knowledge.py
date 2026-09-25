"""知识库层：Schema 注册表 + SQL 示例库。

- SchemaRegistry：表元信息的权威来源，提供「按候选表名 + 1 跳外键」的邻居扩展。
- SQLExampleStore：带标签的 (问题, SQL) 范例集合，供检索层打分。
"""
from __future__ import annotations

from typing import Optional

from .models import SQLExample, TableSchema


class SchemaRegistry:
    def __init__(self, tables: list[TableSchema], dialect: str = "postgres"):
        self.dialect = dialect
        self.tables: dict[str, TableSchema] = {t.name: t for t in tables}

    def get(self, name: str) -> Optional[TableSchema]:
        return self.tables.get(name)

    def names(self) -> list[str]:
        return list(self.tables.keys())

    def link(self, table_names: list[str]) -> list[TableSchema]:
        """根据候选表名，返回 表 + 其外键关联的邻居表（1 跳）。

        作用：LLM 只看到「真正相关」的最小 schema，显著降低幻觉字段概率。
        """
        picked: dict[str, TableSchema] = {}
        for name in table_names:
            t = self.tables.get(name)
            if not t:
                continue
            picked[t.name] = t
            for _col, ref_table, _ref_col in t.foreign_keys:
                neighbor = self.tables.get(ref_table)
                if neighbor:
                    picked[ref_table] = neighbor
        return list(picked.values())


class SQLExampleStore:
    def __init__(self, examples: list[SQLExample]):
        self.examples = examples

    def all(self) -> list[SQLExample]:
        return self.examples

    def get(self, example_id: str) -> Optional[SQLExample]:
        for ex in self.examples:
            if ex.id == example_id:
                return ex
        return None
