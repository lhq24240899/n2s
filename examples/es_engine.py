"""设备日志域的 ES 引擎：问题 -> QueryIR -> ES Query DSL -> 真集群执行。

路由边界（和 SQL 引擎的分工，判据写在这里）：
- 「告警 / 异常 / 日志 / 事件」这类**事件流水**问题 -> ES（本引擎）
- 指标聚合（准时率/收入/利用率…）-> SQL 引擎（PG）

为什么这是"第二种执行引擎"而不是"第二套 prompt"：
  LLM 不参与本路径——问题到 IR 是**确定性规则**（同义词/模式匹配），
  IR 到 ES DSL 是**编译器**。正确性由代码保证，不赌模型输出格式。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from nl2sql.dsl import IRFilter, IRMetric, QueryIR

# 进入 ES 域的关键词（事件流水类问题）
ES_DOMAIN_PATTERNS = ("告警", "异常", "日志", "事件")

LEVEL_WORDS = {"告警": "ERROR", "异常": "ERROR", "故障": "ERROR", "警告": "WARN"}

REGIONS = ("华东", "华南", "华北", "华中", "西南", "西北", "东北")

GROUP_KEYWORDS = {
    "lab_name": ("实验室",),
    "business_line": ("业务线",),
    "region": ("区域", "大区"),
    "device_id": ("设备",),
}

TIME_PATTERNS = [
    (re.compile(r"(最近|近)7 ?天|上周"), "now-7d"),
    (re.compile(r"(最近|近)30 ?天|上个月|上月"), "now-30d"),
]

# 追问信号：只有这类问句才继承上一轮的维度。
# 为什么不像 SQL 路径那样"每轮都继承"？ES 域的问句短且自成一体
# （「各区域最近7天的ERROR告警数量」），而网页上用户会随手点侧边栏示例问题——
# 若无条件继承，刚问完「华东区…」再点「最近7天各级别有多少条日志」就会莫名带上 region=华东。
_FOLLOWUP_RE = re.compile(
    r"(^|[，,。！!\s])(那|那么|还有|再|另外|换(成|个)?|改(成|为))|呢[？?]?\s*$"
)

# 极短且只提到一个维度的问句（如「华南区」「最近30天」）也视为追问
_DIM_WORDS = tuple(REGIONS) + tuple(LEVEL_WORDS) + ("天", "周", "月", "区域", "实验室", "级别")


def is_follow_up(question: str) -> bool:
    """判断是否属于「追问」（决定要不要继承上一轮维度）。"""
    q = (question or "").strip()
    if not q:
        return False
    if _FOLLOWUP_RE.search(q):
        return True
    # 去掉标点后很短、且只含有维度词 -> 例如「华南区」「那华北呢」「最近30天」
    core = re.sub(r"[，,。！!？?\s]", "", q)
    return len(core) <= 6 and any(w in core for w in _DIM_WORDS)


class EsContext:
    """ES/PPL 路径的多轮上下文（与 SQL 路径的 nl2sql/context.py::QueryContext 同构）。

    规则（和 SQL 侧保持一致，别自创一套）：
      - **本轮显式说了的维度覆盖上一轮** —— 所以「那华南区呢」= 只换区域，级别/时间沿用；
      - 本轮没说、上一轮说过的维度**补齐**；
      - **本轮要分组的维度不作为过滤条件继承** —— 例：上一轮「华东区最近7天…」，
        本轮「各区域最近7天…」，若仍继承 region=华东 就只剩一行（SQL 侧实测踩过同一个坑）；
      - scope_filters（行级权限）**不进入上下文**：它是每轮都要重新施加的安全约束，
        不能当作用户说过的维度被继承下去。
    """

    LABELS = {
        "level": "级别", "region": "区域", "ts": "时间窗口", "message": "关键词",
        "lab_name": "实验室", "business_line": "业务线", "device_id": "设备",
    }

    def __init__(self) -> None:
        self.dims: dict[str, tuple[str, Any]] = {}   # field -> (op, value)
        self.group_by: Optional[str] = None

    # ---- 内部 ----

    @staticmethod
    def _index(filters: list[IRFilter]) -> dict[str, tuple[str, Any]]:
        return {f.field: (f.op, f.value) for f in filters}

    def _label(self, field: str) -> str:
        return self.LABELS.get(field, field)

    # ---- 对外 ----

    def inherit(
        self, filters: list[IRFilter], group_by: Optional[str]
    ) -> tuple[list[IRFilter], Optional[str], list[str]]:
        """补齐本轮缺失的维度，返回 (合并后的过滤, 分组, 说明)。"""
        prev = dict(self.dims)
        present = self._index(filters)
        notes: list[str] = []

        for field, (_, value) in present.items():
            if field in prev and prev[field][1] != value:
                notes.append(f"维度替换：{self._label(field)} {prev[field][1]} → {value}")

        merged = dict(prev)
        merged.update(present)          # 本轮显式值覆盖上一轮
        if group_by:
            merged.pop(group_by, None)  # 分组优先：本轮按它分组，就不再当过滤条件继承

        inherited = [f for f in merged if f not in present]
        if inherited:
            notes.append(f"上下文继承：补齐缺失维度 {[self._label(f) for f in inherited]}")

        return [IRFilter(f, op, v) for f, (op, v) in merged.items()], group_by, notes

    def remember(self, filters: list[IRFilter], group_by: Optional[str]) -> None:
        """记录本轮最终维度，供下一轮继承（链式追问：华东 → 那华南呢 → 那最近30天呢）。"""
        self.dims = self._index(filters)
        self.group_by = group_by

    def reset(self) -> None:
        self.dims = {}
        self.group_by = None


class EsQueryEngine:
    """事件流水问数引擎（ES 后端）。ask() 返回与 SQL 引擎同构的 payload。"""

    def __init__(self, backend, index: str = "device_events"):
        self.backend = backend
        self.index = index
        # 多轮上下文：与 SQL 路径的 QueryContext 同构（见 EsContext 的说明）
        self._ctx = EsContext()

    # ---------------- 对外 ----------------

    def ask(self, question: str, scope_filters: Optional[list[IRFilter]] = None) -> dict:
        """scope_filters：数据权限注入点（与 SQL 路径的 PolicyGuard 同源）。

        换执行引擎不能换安全等级——行级权限在 ES 路径同样以过滤条件生效。
        多轮：只有「追问」才继承上一轮维度（见 `is_follow_up`），
        全新问题不继承，避免点几个示例问题就串味。
        """
        parsed, group_by, order_by, order_dir, limit = self._parse(question)
        notes: list[str] = []
        if is_follow_up(question):
            parsed, group_by, notes = self._ctx.inherit(parsed, group_by)
        elif self._ctx.dims:
            notes = ["新问题：不继承上一轮的维度上下文"]
        # 无论是否继承，都用「本轮最终的维度」覆盖上下文，保证链式追问（华东→那华南呢→那最近30天呢）
        self._ctx.remember(parsed, group_by)

        ir = self._assemble(parsed, group_by, order_by, order_dir, limit,
                            scope_filters=scope_filters, notes=notes)
        err = ir.validate()
        if err:
            return {"type": "clarification", "message": f"未能理解该日志类问题：{err}", "engine": "es"}
        try:
            dsl = ir.to_es_dsl()
            cols, rows = self.backend.query(self.index, dsl, ir)
        except Exception as e:  # noqa: BLE001 - ES 不可用时明确报错（上层可降级）
            return {"type": "error", "message": f"ES 查询失败: {e}", "engine": "es"}

        ppl = ir.to_ppl()  # 编译产物：同一份 IR 也能编译成 PPL
        # PPL 双路：连 OpenSearch 时真执行；普通 ES 上 _plugins/_ppl 不存在 -> 降级为"仅编译"
        # adaptations：编译层做过的方言适配（中文字面量→ASCII 编码字段、相对时间→绝对时间）。
        # 透出去是为了让用户看懂"为什么 PPL 语句里的字段名和问题里的不一样"。
        adaptations = ir.ppl_adaptations()
        ppl_payload: dict = {"query": ppl, "status": "compiled-only", "adaptations": adaptations}
        try:
            ppl_cols, ppl_rows = self.backend.execute_ppl(ppl, ir=ir)
            ppl_payload.update(status="executed", columns=ppl_cols, rows=ppl_rows)
        except Exception as e:  # noqa: BLE001 - PPL 不可用不影响主结果
            ppl_payload["status_detail"] = str(e)[:200]

        return {
            "type": "result",
            "engine": "es",
            "columns": cols,
            "rows": rows,
            "es_dsl": dsl,
            "ppl": ppl_payload,
            "entities": {"index": ir.index, "filters": [(f.field, f.op, f.value) for f in ir.filters]},
            "reasons": [f"IR->ES DSL 编译（{len(ir.filters)} 个过滤, "
                        f"{'按 ' + ir.group_by + ' 分组' if ir.group_by else '全局聚合'})",
                        f"PPL: {ppl_payload['status']}"]
                       + [f"PPL 适配：{a}" for a in adaptations]
                       + [f"多轮：{n}" for n in ir.context_notes],
            "row_count": len(rows),
            "empty": len(rows) == 0,
        }

    def reset_context(self) -> None:
        """清空多轮上下文（与 SQL 引擎同名方法，供"清空对话"按钮统一调用）。"""
        self._ctx.reset()

    def is_es_domain(self, question: str) -> bool:
        """是否属于事件流水域（决定路由到 ES 还是 SQL）。"""
        return any(p in question for p in ES_DOMAIN_PATTERNS)

    # ---------------- 问题 -> IR（确定性规则，不靠 LLM） ----------------

    def _parse(self, question: str) -> tuple[list[IRFilter], Optional[str], Optional[str], str, int]:
        """问题 -> (过滤条件, 分组字段, 排序字段, 排序方向, 条数)。纯函数，不碰上下文。"""
        filters: list[IRFilter] = []

        # 1) 级别：告警/异常 -> ERROR；警告 -> WARN（未提及则不过滤）
        for word, level in LEVEL_WORDS.items():
            if word in question:
                filters.append(IRFilter("level", "term", level))
                break

        # 2) 区域
        for r in REGIONS:
            if r in question:
                filters.append(IRFilter("region", "term", r))
                break

        # 3) 时间窗口（ES 的 now-7d 直接支持 date math）
        for pattern, window in TIME_PATTERNS:
            if pattern.search(question):
                filters.append(IRFilter("ts", "gte", window))
                break

        # 4) 全文检索（"包含 温度超限 的日志"）
        m = re.search(r"包含[「'\"]?([^'」\"，。,]+)[」'\"]?", question)
        if m and "包含" in question:
            filters.append(IRFilter("message", "match", m.group(1).strip()))

        # 5) 分组维度
        group_by: Optional[str] = None
        for field, words in GROUP_KEYWORDS.items():
            if any(w in question for w in words):
                group_by = field
                break

        # 6) 指标：日志域默认计数
        if group_by is None and any(w in question for w in ("次数", "数量", "多少", "总数")):
            group_by = "level"   # 问"多少条日志"又不指定分组 -> 按级别给分布，更有信息量

        # 7) 排名
        order_by, order_dir, limit = None, "desc", 20
        if re.search(r"最多|最高|排名", question):
            order_by, limit = "cnt", (1 if not re.search(r"三|3", question) else 3)
        elif re.search(r"最少|最低", question):
            order_by, order_dir, limit = "cnt", "asc", (1 if not re.search(r"三|3", question) else 3)
        elif re.search(r"三个|3个|前3|前三", question):
            order_by, limit = "cnt", 3

        return filters, group_by, order_by, order_dir, limit

    def _assemble(
        self,
        parsed: list[IRFilter],
        group_by: Optional[str],
        order_by: Optional[str],
        order_dir: str,
        limit: int,
        scope_filters: Optional[list[IRFilter]] = None,
        notes: Optional[list[str]] = None,
    ) -> QueryIR:
        """装配 IR。行级权限过滤放最前：它是安全约束，不能被后续规则挤掉。"""
        return QueryIR(
            index=self.index,
            filters=list(scope_filters or []) + list(parsed),
            group_by=group_by,
            metrics=[IRMetric("cnt", "count")],
            order_by=order_by if group_by else None,
            order_dir=order_dir,
            limit=limit,
            context_notes=list(notes or []),
        )

    def build_ir(self, question: str, scope_filters: Optional[list[IRFilter]] = None) -> QueryIR:
        """单轮解析（不涉及上下文）——评估脚本与单测用这个，行为与历史版本一致。"""
        parsed, group_by, order_by, order_dir, limit = self._parse(question)
        return self._assemble(parsed, group_by, order_by, order_dir, limit,
                              scope_filters=scope_filters)


# SQL 行级权限（policy.RowFilter）-> ES 文档字段。
# 设备日志文档上 region / business_line(=业务线 code) 与关系库同名同义。
_POLICY_FIELD_MAP = {
    ("labs", "region"): "region",
    ("business_lines", "code"): "business_line",
}


def scope_filters_from_policy(policy) -> list[IRFilter]:
    """把数据权限翻译成 ES 过滤：换引擎不换安全等级（与 SQL 路径的 PolicyGuard 同源）。"""
    out: list[IRFilter] = []
    for rf in getattr(policy, "row_filters", ()):  # type: ignore[attr-defined]
        es_field = _POLICY_FIELD_MAP.get((rf.table, rf.column))
        if es_field and rf.values:
            out.append(IRFilter(es_field, "terms", list(rf.values)))
    return out


class HybridRouter:
    """按问题域路由：事件流水 -> ES；指标/口径/文档 -> 原引擎。

    ES 不可用或问题不属于 ES 域时，**永远回落到原引擎**——
    加第二种执行引擎不能让既有能力变脆弱。
    """

    def __init__(
        self,
        sql_engine,
        es_engine: Optional[EsQueryEngine],
        scope_filters: Optional[list[IRFilter]] = None,
    ):
        self.sql_engine = sql_engine
        self.es_engine = es_engine
        self.scope_filters = scope_filters or []

    def ask(self, question: str) -> dict:
        if self.es_engine is not None and self.es_engine.is_es_domain(question):
            out = self.es_engine.ask(question, scope_filters=self.scope_filters)
            if out.get("type") != "error":     # ES 失败时回落 SQL 引擎兜底
                return out
            out_fallback = self.sql_engine.ask(question)
            out_fallback.setdefault("reasons", []).append("ES 不可用，已回落 SQL 引擎")
            return out_fallback
        return self.sql_engine.ask(question)

    # 透传原引擎的会话能力（reset/pending 等）
    def __getattr__(self, name):
        return getattr(self.sql_engine, name)
