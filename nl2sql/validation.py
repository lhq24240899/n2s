"""SQL 校验层：用 sqlglot 做 AST 级静态校验，比正则可靠得多。

校验顺序（任一不通过即返回错误，绝不把脏 SQL 交给执行层）：
1. 非空；
2. 能被目标方言解析（语法错误直接拦下）；
3. 不含任何写操作（INSERT/UPDATE/DELETE/DROP/...）；
4. 顶层必须是 SELECT / WITH ... SELECT；
5. 表白名单：引用的表必须在 allowed_tables 内；
6. 列白名单：引用的列必须存在于对应表中（拦下「幻觉字段」）。

第 5/6 步是防幻觉的核心：LLM 即使编造字段名，也会在 AST 层面被精确拦截。
"""
from __future__ import annotations

from typing import Optional

import sqlglot
from sqlglot import exp

from .knowledge import SchemaRegistry

# 禁止的写操作 AST 节点
_FORBIDDEN = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Create,
    exp.Grant,
)


class SQLValidator:
    def __init__(self, registry: SchemaRegistry, dialect: str = "postgres"):
        self.registry = registry
        self.dialect = dialect

    def validate(self, sql: str, allowed_tables: list[str]) -> Optional[str]:
        if not sql or not sql.strip():
            return "SQL 为空"

        try:
            parsed = sqlglot.parse_one(sql, dialect=self.dialect)
        except Exception as e:  # noqa: BLE001 - 解析失败需明确反馈给 LLM
            return f"SQL 解析失败: {e}"

        if parsed is None:
            return "SQL 解析失败: 空语句"

        # 写操作拦截
        for cls in _FORBIDDEN:
            if parsed.find(cls):
                return f"包含禁用写操作: {cls.__name__}"

        # 必须是只读查询
        if not isinstance(parsed, (exp.Select, exp.With)):
            return "不是 SELECT 查询"

        # 表白名单
        referenced = {t.name for t in parsed.find_all(exp.Table)}
        unknown = referenced - set(allowed_tables)
        if unknown:
            return f"引用了未授权表: {sorted(unknown)}"

        # 列白名单（幻觉字段拦截）
        table_cols = {name: set(t.columns.keys()) for name, t in self.registry.tables.items()}
        for col in parsed.find_all(exp.Column):
            cname = col.name
            tbl = col.table  # 限定符，可能为空
            if tbl:
                if tbl in table_cols and cname not in table_cols[tbl]:
                    return f"列不存在: {tbl}.{cname}"
            else:
                if not any(cname in table_cols[t] for t in allowed_tables):
                    return f"列不存在: {cname}"

        return None
