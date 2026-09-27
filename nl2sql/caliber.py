"""口径一致性校验：生成的 SQL 有没有**真的**按语义层的口径来。

为什么需要这一层
----------------
口径目前是**提示级**约束（写进提示词当业务约束），而模型可以不遵守。真机实测到的三起：

1. 「某汽车客户的合同金额是多少」-> 客户名被整个丢掉（**实体过滤没落地**）-> 返回全库 5,450,000；
2. 「各客户的合同金额是多少」-> 返回 `customer_id` 而不是 `customers.name`
   （**分组展示列没按口径给**，`GROUP_COLUMNS` 里明确要求 name）；
3. 该带 `settled_status='已开票'` 的场合漏掉这个过滤（**指标口径的关键过滤缺失**）。

这三类 SQL **都能通过静态校验**（只读 / 表白名单 / EXPLAIN）：语法没毛病、表也合法，
只是答的**不是那个口径**。静态校验管"安全"，这一层管"对不对口径"——两件事。

定位
----
与 `review.py`（答案评审）、`condense.py`（追问改写）同一层：**引擎出口的零成本规则**
（纯函数、不调模型）。本档只**标注**、不干预——违规写进 payload 的 `caliber`，
界面如实提示。"带反馈重生成""改用 sql_hint 模板"那两档，等误报率观察清楚了再上。

判定原则：**宁可漏报，不可误报**（这是提示级校验，误报会让真正的问题被噪声淹没）。
所以：解析失败不判、业务线取值不判（code 与中文名都合法，硬判必误报）。
"""
from __future__ import annotations

from typing import Any, Optional

import sqlglot
from sqlglot import exp


def _tables(parsed) -> set[str]:
    return {t.name.lower() for t in parsed.find_all(exp.Table) if t.name}


def _colname(ref: str) -> str:
    """`customers.name` / `name` 两种写法都取出 `name`。

    （不要用 `partition(".")[2]`：不带表前缀时它会得到空串，于是永远匹配不上 —— 实测踩过。）
    """
    return str(ref).split(".")[-1].lower()


def _columns(parsed) -> set[str]:
    return {c.name.lower() for c in parsed.find_all(exp.Column) if c.name}


def _eq_filters(parsed) -> list[tuple[str, str]]:
    """收集 `列 = 字面量`（含单元素 IN）形式的过滤，返回 [(列名小写, 值)]。

    用 AST 而不是字符串匹配：引号、空格、大小写、列别名都会骗过 grep
    （`policy.py` 的列级拦截、`validation.py` 的静态校验也都是这么做的）。
    """
    out: list[tuple[str, str]] = []
    for node in parsed.find_all(exp.EQ):
        for col, lit in ((node.left, node.right), (node.right, node.left)):
            if isinstance(col, exp.Column) and isinstance(lit, exp.Literal):
                if isinstance(lit.this, str):
                    out.append((col.name.lower(), lit.this))
    for node in parsed.find_all(exp.In):
        if not isinstance(node.this, exp.Column):
            continue
        for lit in node.expressions or []:
            if isinstance(lit, exp.Literal) and isinstance(lit.this, str):
                out.append((node.this.name.lower(), lit.this))
    return out


def check_caliber(
    sql: str,
    *,
    entities: Optional[dict] = None,
    group_by: Optional[str] = None,
    guard: Any = None,
    entity_columns: Optional[dict] = None,
    group_columns: Optional[dict] = None,
    group_column_accept: Optional[dict] = None,
    question: str = "",
    dialect: str = "postgres",
) -> list[str]:
    """返回违规说明列表；空列表 = 通过。

    - sql 为空 / 解析失败 -> 返回空（那是静态校验层的事，这里不重复报错、也不误报）
    - guard：`nl2sql.semantic.MetricGuard`，声明该指标的口径硬约束（数据驱动）
    """
    if not sql or not str(sql).strip():
        return []
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:  # noqa: BLE001 - 解析不了就不判（宁可漏报）
        return []
    if parsed is None:
        return []

    ents = entities or {}
    problems: list[str] = []

    # 1) 实体过滤必须落地：识别出的**取值**要真的出现在 SQL 里。
    #    只查「客户 / 区域」这两个中文取值——它们只有一种规范写法（这也是 Glossary 强制下发的）。
    #    业务线**不查**：库里 code（reliability）与中文名（可靠性与环境试验）都算合法写法，
    #    硬判会误报（宁可漏报）。
    raw = str(sql)
    for key in ("customer", "region"):
        value = ents.get(key)
        if value and str(value) not in raw:
            problems.append(
                f"口径：识别出 {key}={value}，但 SQL 里没有出现这个取值（过滤没落地）"
            )

    # 2) 分组维度必须输出展示列：问「各客户」不能只给 id。
    #    （真机踩过：`SELECT c.customer_id ... GROUP BY c.customer_id` -> 用户看不懂是哪家客户）
    #    允许多个可接受写法：同一维度在不同主题域可能落到不同物理列
    #    （`business_line` 在营收域是 `business_segment`，见 SemanticLayer.GROUP_COLUMN_ACCEPT）。
    target = (group_columns or {}).get(group_by) if group_by else None
    if target:
        extras = (group_column_accept or {}).get(group_by) or ()
        candidates = [target, *extras]
        cols = _columns(parsed)
        if not any(_colname(t) in cols for t in candidates):
            problems.append(
                f"口径：本轮按「{group_by}」分组，SQL 应输出展示列 {target}，但 SELECT 里没有它"
            )

    # 3) 指标口径的硬约束（见 MetricGuard）
    if guard is not None:
        triggers = tuple(getattr(guard, "trigger", ()) or ())
        # 3a) must_filter：**按问法启用**——口径允许"可按 settled_status 过滤"，
        #     所以只有问题里真的提到「已开票」时才要求这个过滤（否则就是误报）。
        if not triggers or any(t in (question or "") for t in triggers):
            filters = _eq_filters(parsed)
            for column, values in dict(getattr(guard, "must_filter", {}) or {}).items():
                col = str(column).split(".")[-1].lower()
                if not any(c == col and v in values for c, v in filters):
                    problems.append(
                        f"口径：问题里提到「{'、'.join(values)}」，"
                        f"SQL 却没有 {column} 等于该值的过滤"
                    )
        # 3b) forbid_tables：**恒定判定**，不设触发词 —— "该口径不得 JOIN 某张表"是定义本身
        #     的一部分（一对多连接会把金额按行重复累加），与问法无关。
        used = _tables(parsed)
        hit = [t for t in (getattr(guard, "forbid_tables", ()) or ()) if str(t).lower() in used]
        if hit:
            problems.append(
                f"口径：该指标的口径明令不要 JOIN {'、'.join(hit)}"
                f"（一对多会把金额按行重复累加）"
            )
    return problems


def check_mapped(sql: str, mapped, layer, dialect: str = "postgres") -> list[str]:
    """便捷入口：从 MappedQuery + 语义层取齐检查所需的上下文（引擎出口就调这个）。"""
    if mapped is None:
        return []
    ents = dict(getattr(mapped, "entities", None) or {})
    metric = getattr(mapped, "metric", None)
    return check_caliber(
        sql,
        entities=ents,
        group_by=ents.get("group_by"),
        guard=(getattr(layer, "metric_guards", None) or {}).get(getattr(metric, "id", None)),
        entity_columns=getattr(layer, "entity_columns", None),
        group_columns=getattr(layer, "GROUP_COLUMNS", None),
        group_column_accept=getattr(layer, "GROUP_COLUMN_ACCEPT", None),
        question=getattr(mapped, "original", "") or getattr(mapped, "normalized", ""),
        dialect=dialect,
    )
