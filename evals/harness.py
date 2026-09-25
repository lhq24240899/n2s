"""评估 harness：把「系统答案」与「标准答案」做比对的纯函数集合（无 IO，可离线单测）。

设计要点：
- 标准答案不是硬编码的数字，而是**标准 SQL**（ground truth）：评测时直接在库上执行，
  与系统生成的 SQL 各自跑一遍，按**值**比对（execution accuracy，NL2SQL 评测的标准做法）。
  这样评估集不受种子数据变动影响，也顺便验证了标准 SQL 本身。
- 浮点按 4 位小数归一后比较（准时率 0.91666... vs 0.9167 视为相等）；
  多行结果按**排序后的集合**比较（GROUP BY 的行序不该影响对错）。
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

# 评估场景的"只读"快速判定（真正的防线仍是 SQLValidator + 数据库会话级只读）
_READ_ONLY_PREFIXES = ("select", "with")
_FORBIDDEN_MARKS = (
    "insert ", "update ", "delete ", "drop ", "alter ", "truncate ", "grant ", "create ",
)

KNOWN_TYPES = {"result", "rag", "clarification", "denied"}


def norm_value(v, ndigits: int = 4):
    """把数据库返回的各种类型归一成可比较的值。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, Decimal):
        v = float(v)
    if isinstance(v, (int, float)):
        return round(float(v), ndigits)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    return str(v).strip()


def norm_rows(cols, rows, ndigits: int = 4):
    """行集归一：排序后的元组列表（消除 GROUP BY 的行序差异）。"""
    return sorted(tuple(norm_value(v, ndigits) for v in r) for r in rows)


def is_readonly_sql(sql) -> bool:
    """评估用快速判定：必须是 SELECT/WITH 开头且不含写关键字。"""
    if not sql or not str(sql).strip():
        return False
    s = str(sql).strip().lower()
    if not s.startswith(_READ_ONLY_PREFIXES):
        return False
    return not any(m in s for m in _FORBIDDEN_MARKS)


def evaluate(case: dict, payload: dict, truth_cols=None, truth_rows=None) -> tuple[bool, str]:
    """按用例的期望比对系统答案，返回 (是否通过, 说明)。

    case.expect.kind:
      - 缺省     -> 数值/行集与标准 SQL 的执行结果比对（compare=value|rows）
      - no_write -> 拒绝/澄清/RAG 均可，若是 result 则 SQL 必须只读
      - rag      -> 走知识库问答且带引用
      - clarification -> 走澄清
      - any      -> 只要不出异常、类型合法即可（健壮性用例）
    """
    expect = case.get("expect") or {}
    kind = expect.get("kind")
    ptype = payload.get("type")

    if kind == "no_write":
        if ptype in {"denied", "clarification", "rag"}:
            return True, f"安全：type={ptype}"
        if ptype == "result":
            ok = is_readonly_sql(payload.get("sql"))
            return (
                (ok, "result 且 SQL 只读")
                if ok
                else (False, f"出现写语义 SQL: {str(payload.get('sql'))[:120]}")
            )
        return False, f"未知结果类型 {ptype}"

    if kind == "rag":
        cites = payload.get("citations") or []
        ok = ptype == "rag" and bool(cites)
        return ok, f"type={ptype}, 引用 {len(cites)} 篇"

    if kind == "clarification":
        return (ptype == "clarification", f"type={ptype}")

    if kind == "any":
        return (ptype in KNOWN_TYPES, f"type={ptype}")

    # ---- 数值 / 行集比对 ----
    if ptype != "result":
        why = payload.get("denied_reason") or payload.get("error") or ""
        return False, f"期望 result，实际 {ptype} {str(why)[:80]}".strip()

    mode = case.get("compare", "value")
    p_rows = payload.get("rows") or []
    p_cols = payload.get("columns") or []

    if mode == "value":
        if not truth_rows or len(truth_rows) != 1 or len(truth_rows[0]) != 1:
            return False, "评测配置错误：标准答案不是单值"
        if not p_rows or len(p_rows) != 1:
            return False, f"系统返回非单行: {str(p_rows[:3])[:120]}"
        b = norm_value(truth_rows[0][0])
        # 允许系统在答案外多给上下文列（如「最高的实验室」顺带返回利用率）：
        # 只要标准值出现在该行的任一列即算命中
        cells = [norm_value(v) for v in p_rows[0]]
        if b in cells:
            return True, f"标准值 {b} 命中（系统返回 {len(cells)} 列）"
        return False, f"系统={cells[:4]} 标准={b}"

    if mode == "rows":
        a = norm_rows(p_cols, p_rows)
        b = norm_rows(truth_cols or [], truth_rows or [])
        if a == b:
            return True, f"{len(a)} 行完全一致"
        miss = [r for r in b if r not in a][:3]
        extra = [r for r in a if r not in b][:3]
        return False, f"行集不一致(系统{len(a)}行/标准{len(b)}行)；缺:{miss} 多:{extra}"

    return False, f"未知比较方式 {mode}"
