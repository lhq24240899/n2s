"""数据权限策略：把「谁能看到哪些数据」落到链路的三个位置。

为什么单独一层？
  认证（auth.py）只回答"你是谁、能做什么动作"。
  但企业里真正敏感的是**数据本身**：同一条"区域收入"问题，华东大区经理和
  集团分析师应当得到不同口径的结果；手机号/客户名称这类字段不该给所有人。

三处落地（前两处"防泄漏"，第三处"防绕过"）：

  ① 生成前 - constrain_schemas()：把无权表**从 prompt 里摘掉**。
     LLM 看不到 `customers.phone`，自然写不出它——比事后拦截更干净。

  ② 生成前 - check_metric()：指标级拒绝（如 viewer 不得问收入类指标）。
     这类拒绝应当是明确的业务提示，而不是让 LLM 硬生成一条被拦的 SQL。

  ③ 生成后 - post_sql()：
     - 列级：AST 检查是否引用未授权字段，命中则作为"错误反馈"让 LLM 重写；
     - 行级：用 sqlglot 把过滤条件**注入 SQL 的 WHERE**（而不是查完再在
       Python 里过滤——那样行数、聚合值都已经算错了）。

行级过滤为什么这么实现：
  直接字符串拼 `AND region='华东'` 会踩到子查询/别名/CTE 的坑；
  这里解析成 AST，找到**真正引用该表的那层 SELECT**，用它的别名构造谓词，
  再序列化回 SQL。若整条 SQL 根本没引用该表（例如只查 equipment），
  则如实返回一条"未生效"提示，绝不假装过滤成功。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Sequence

import sqlglot
from sqlglot import exp

from .auth import Principal
from .models import TableSchema
from .semantic import Metric

_log = logging.getLogger("nl2sql.policy")


class PolicyViolation(Exception):
    """数据权限拒绝。接口层映射为 403，并原样返回 reason 便于前端提示。"""

    status_code = 403

    def __init__(self, reason: str, *, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class RowFilter:
    """行级过滤：限定某张表某列只能取给定值集合。"""

    table: str
    column: str
    values: tuple[str, ...]


# 各角色的默认数据权限。真实项目里应存在配置库/权限系统，这里以常量集中表达，
# 便于面试时一眼看清"权限规则长什么样"。
DEFAULT_POLICIES: dict[str, dict] = {
    "viewer": {
        "allowed_metrics": None,          # 指标不额外限制（但无 query:ask，问不了数）
        "row_filters": (),
    },
    "analyst": {
        "allowed_metrics": None,
        "row_filters": (),
    },
    "admin": {
        "allowed_metrics": None,
        "row_filters": (),
    },
}

# 敏感字段：任何角色都不能让 LLM 直接查出来（示例：客户联系方式）
SENSITIVE_COLUMNS: frozenset[str] = frozenset(
    {"phone", "contact_phone", "email", "id_card", "address"}
)


@dataclass(frozen=True)
class DataPolicy:
    """某一次请求生效的数据权限（由 Principal + 角色默认策略合成）。"""

    allowed_tables: frozenset[str] | None = None
    denied_columns: frozenset[str] = SENSITIVE_COLUMNS
    allowed_metrics: frozenset[str] | None = None
    row_filters: tuple[RowFilter, ...] = ()
    dialect: str = "postgres"
    notes: tuple[str, ...] = field(default=())

    @classmethod
    def from_principal(cls, principal: Principal, dialect: str = "postgres") -> "DataPolicy":
        """把身份翻译成数据权限：令牌里的 regions/business_lines -> 行级过滤。"""
        filters: list[RowFilter] = []
        if principal.regions:
            filters.append(RowFilter("labs", "region", tuple(sorted(principal.regions))))
        if principal.business_lines:
            filters.append(
                RowFilter("business_lines", "code", tuple(sorted(principal.business_lines)))
            )
        base = DEFAULT_POLICIES.get(principal.role.value, {})
        return cls(
            allowed_tables=None,
            denied_columns=SENSITIVE_COLUMNS,
            allowed_metrics=base.get("allowed_metrics"),
            row_filters=tuple(filters),
            dialect=dialect,
        )

    @property
    def is_scoped(self) -> bool:
        """是否配置了行级限制（用于响应里标注"结果已按你的数据范围过滤"）。"""
        return bool(self.row_filters)

    def describe(self) -> list[str]:
        out = [f"行级过滤: {f.table}.{f.column} IN {list(f.values)}" for f in self.row_filters]
        if self.allowed_tables is not None:
            out.append(f"可见表: {sorted(self.allowed_tables)}")
        out.append(f"敏感字段一律不下发: {sorted(self.denied_columns)}")
        return out


class PolicyGuard:
    """把 DataPolicy 挂到 pipeline 的三个挂点上（pipeline 只需调用，不关心策略细节）。"""

    def __init__(self, policy: DataPolicy):
        self.policy = policy
        self.dialect = policy.dialect
        # 一次查询中发生的授权事实，供响应与审计使用
        self.applied: list[str] = []

    # ---------------- ① 生成前：收窄可见表 ----------------

    def constrain_schemas(self, schemas: list[TableSchema]) -> list[TableSchema]:
        allowed = self.policy.allowed_tables
        if allowed is None:
            return schemas
        kept = [s for s in schemas if s.name in allowed]
        dropped = [s.name for s in schemas if s.name not in allowed]
        if dropped:
            self.applied.append(f"已隐藏无权表: {sorted(dropped)}")
        return kept

    # ---------------- ② 生成前：指标级拒绝 ----------------

    def check_metric(self, metric: Optional[Metric]) -> None:
        allowed = self.policy.allowed_metrics
        if metric is None or allowed is None:
            return
        if metric.id not in allowed:
            raise PolicyViolation(
                f"你的角色无权查询指标「{metric.name}」",
                detail=f"允许的指标: {sorted(allowed)}",
            )

    # ---------------- ③ 生成后：列级拦截 + 行级注入 ----------------

    def post_sql(self, sql: str) -> tuple[str, Optional[str]]:
        """返回 (改写后的 SQL, 错误反馈)。错误反馈非空时上层应带反馈重生成。"""
        if not sql or not sql.strip():
            return sql, None

        denied = self._find_denied_column(sql)
        if denied:
            return sql, (
                f"SQL 引用了未授权字段 {denied}。该字段属于敏感信息，禁止出现在查询中；"
                f"请改写 SQL，不要选择或过滤这些字段。"
            )

        if not self.policy.row_filters:
            return sql, None

        rewritten, notes = apply_row_filters(sql, self.policy.row_filters, self.dialect)
        self.applied.extend(notes)
        return rewritten, None

    def _find_denied_column(self, sql: str) -> Optional[str]:
        """AST 级列级权限检查：命中敏感字段即拒绝（宁可误拦，不可漏放）。"""
        denied = self.policy.denied_columns
        if not denied:
            return None
        try:
            parsed = sqlglot.parse_one(sql, dialect=self.dialect)
        except Exception as e:  # noqa: BLE001
            # ⚠️ fail-closed：配了敏感字段但 SQL 解析不了时**保守拒绝**，不能放行。
            #    早期实现是"解析失败就跳过列级检查、交给 validator 报错"，可回退路径
            #    并不一定调用 validator（graph 的模板回退就漏了）—— 于是一条语法坏的 SQL
            #    能连语法带权限一起绕过。真机上正是这样暴露的：示例 SQL 少写一个 `) x`，
            #    解析失败 -> 列级检查静默跳过 -> 含被禁字段的 SQL 被直接执行并返回数据。
            return f"<无法解析：{type(e).__name__}: {str(e)[:80]}>"
        if parsed is None:
            return "<无法解析：空语句>"
        for col in parsed.find_all(exp.Column):
            qualified = f"{col.table}.{col.name}" if col.table else col.name
            if qualified in denied or col.name.lower() in {d.lower() for d in denied}:
                return qualified
        return None


# ---------------------------------------------------------------------------
# 行级过滤注入（sqlglot AST 改写）
# ---------------------------------------------------------------------------

def apply_row_filters(
    sql: str,
    filters: Sequence[RowFilter],
    dialect: str = "postgres",
) -> tuple[str, list[str]]:
    """把行级过滤注入到「真正引用该表的那层 SELECT」的 WHERE 中。

    返回 (SQL, 说明)。找不到对应表时**不改写**，并在说明里如实标注未生效。
    """
    if not filters:
        return sql, []

    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except Exception as e:  # noqa: BLE001
        return sql, [f"行级过滤未生效（SQL 解析失败: {e}）"]

    if parsed is None:
        return sql, ["行级过滤未生效（空语句）"]

    notes: list[str] = []
    changed = False
    for rf in filters:
        target = _locate_select_with_table(parsed, rf.table)
        if target is None:
            notes.append(
                f"行级过滤未生效（SQL 未引用表 {rf.table}）：{rf.table}.{rf.column}"
            )
            continue
        select, alias = target
        predicate = _in_predicate(alias, rf.column, rf.values)
        where = select.args.get("where")
        if where is None:
            select.set("where", exp.Where(this=predicate))
        else:
            select.set("where", exp.Where(this=exp.and_(where.this, predicate)))
        changed = True
        notes.append(
            f"已注入行级过滤: {rf.table}.{rf.column} IN {list(rf.values)}"
        )

    if not changed:
        return sql, notes
    return parsed.sql(dialect=dialect), notes


def _locate_select_with_table(node: exp.Expression, table_name: str) -> Optional[tuple[exp.Select, str]]:
    """找到引用 table_name 的那层 SELECT，并返回其别名。

    从外层往里找（外层优先），因为过滤条件加在最外层最直观、也最不容易被
    子查询的 LIMIT/聚合语义影响。
    """
    target = table_name.lower()
    for select in node.find_all(exp.Select):
        alias = _table_alias_in_scope(select, target)
        if alias:
            return select, alias
    return None


def _table_alias_in_scope(select: exp.Select, table_name: str) -> Optional[str]:
    """只看该层自己的 FROM / JOIN，不下钻子查询。"""
    candidates: list[exp.Table] = []
    from_ = select.args.get("from")
    if from_ is not None and isinstance(from_.this, exp.Table):
        candidates.append(from_.this)
    for join in select.args.get("joins") or []:
        if isinstance(join.this, exp.Table):
            candidates.append(join.this)
    for t in candidates:
        if t.name.lower() == table_name:
            return t.alias_or_name
    return None


def _in_predicate(alias: str, column: str, values: Sequence[str]) -> exp.Expression:
    col = exp.column(column, table=alias)
    if len(values) == 1:
        return exp.EQ(this=col, expression=exp.Literal.string(values[0]))
    return exp.In(
        this=col,
        expressions=[exp.Literal.string(v) for v in values],
    )
