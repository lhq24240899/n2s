"""QueryIR：方言无关的查询中间表示 —— 一份 IR，编译到多种执行语言。

为什么要有 IR（对应 JD「自然语言转 SQL/PPL/DSL」的正确姿势）：
- 让 LLM 直接写 PPL/JSON = 每种语言各赌一次格式化（括号、保留字、字段名全靠模型）；
- 有了 IR，LLM/规则只负责**填槽**，各执行语言的语法正确性由**编译器**保证；
- 校验、权限、审计都能作用在 IR 上 —— 换执行引擎不换安全等级。

当前提供两个编译目标：
- `to_es_dsl()`   → Elasticsearch Query DSL（真集群执行：阿里云 ES）
- `to_ppl()`      → OpenSearch PPL（连 OpenSearch 时经 `_plugins/_ppl` **真执行**；
  连普通 ES 时执行器探测不到该端点，自动降级为"仅编译产物"）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# ES date math（now-7d / now-30d / now-2h）-> PPL 用的绝对时间字符串。
# OpenSearch PPL 的 where 不支持 ES 的 date math，编译器把相对窗口换算成执行时刻的绝对时间。
_NOW_REL = re.compile(r"^now-(\d+)([dhw])$")
_UNIT_SECONDS = {"d": 86400, "h": 3600, "w": 604800}

# ---------------------------------------------------------------------------
# 编译层：非 ASCII 字面量的方言适配
#
# 真机踩坑（Aiven OpenSearch 3.6.0）：PPL 里**任何中文字面量**都会 500 ——
#   {"details": "Failed to encode '华东' in character set 'ISO-8859-1'", "type": "CalciteException"}
# 实测换了 U&'\+534E' 转义、双引号、like()、match()、显式 charset=utf-8 请求头，全部无效：
# 这是 PPL 引擎（Calcite）用 ISO-8859-1 编码字面量的自身限制，不是查询写错了。
#
# 解法与 date math 的适配同一思路：**方言差异全部消化在编译层**，IR 与语义层保持干净。
# 索引里为需要过滤的中文字段准备了 ASCII 伴生字段（见 examples/setup_es_demo.py 的 MAPPING），
# PPL 编译时把中文过滤改写成对编码字段的过滤；DSL 编译不受影响（ES 的 JSON 走 UTF-8，无此问题）。
# 若字面量不在映射表里，则保持原样并由执行器给出可操作报错（不静默改语义）。
# ---------------------------------------------------------------------------
ASCII_CODE_FIELDS: dict[str, tuple[str, dict[str, str]]] = {
    "region": ("region_code", {
        "华东": "east", "华南": "south", "华北": "north",
        "华中": "central", "西北": "northwest", "西南": "southwest",
    }),
    "lab_name": ("lab_code", {
        "上海集成电路实验室": "sh_ic", "深圳可靠性实验室": "sz_rel", "北京电磁兼容实验室": "bj_emc",
    }),
    "business_line": ("bl_code", {
        "集成电路测试与分析": "ic", "可靠性与环境试验": "reliability",
        "电磁兼容检测": "emc", "计量服务": "calibration", "数据科学分析与评价": "data_science",
    }),
    # value 是「包含」检索提取出的关键词，用「包含关系」双向匹配文档文案
    "message": ("msg_code", {
        "温度超限告警": "temp_high", "通信中断异常": "comm_lost",
        "校准偏移提醒": "calib_drift", "散热风扇转速异常": "fan_speed",
        "设备自检正常": "self_check_ok", "例行巡检完成": "patrol_done",
    }),
}


def _lookup_code(mapping: dict[str, str], value: str) -> Optional[str]:
    """在映射表里找编码：先精确，再双向包含（应对'温度超限' vs '温度超限告警'）。"""
    if value in mapping:
        return mapping[value]
    for k, code in mapping.items():
        if value in k or k in value:
            return code
    return None



def _abs_since(value: str, now: Optional[datetime] = None) -> str:
    """把 now-7d 这类相对时间换算成 'YYYY-MM-DD HH:MM:SS'；不是相对时间则原样返回。"""
    m = _NOW_REL.match(str(value))
    if not m:
        return str(value)
    seconds = int(m.group(1)) * _UNIT_SECONDS[m.group(2)]
    dt = (now or datetime.now(timezone.utc)) - timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class IRFilter:
    """过滤条件。op: term(精确) | terms(集合) | gte | lte | match(全文)"""

    field: str
    op: str
    value: Any

    def to_es(self) -> dict:
        if self.op == "term":
            return {"term": {self.field: self.value}}
        if self.op == "terms":
            return {"terms": {self.field: list(self.value)}}
        if self.op in ("gte", "lte"):
            return {"range": {self.field: {self.op: self.value}}}
        if self.op == "match":
            return {"match": {self.field: self.value}}
        raise ValueError(f"未知过滤 op: {self.op}")

    def to_ppl(self, ascii_safe: bool = True) -> str:
        return self.to_ppl_with_note(ascii_safe=ascii_safe)[0]

    def to_ppl_with_note(self, ascii_safe: bool = True) -> tuple[str, Optional[str]]:
        """编译成 PPL 片段，返回 (语句, 适配说明或 None)。"""
        field, value, note = self._ascii_adapt(ascii_safe)

        if self.op == "term":
            v = f"'{value}'" if isinstance(value, str) else value
            return f"{field} = {v}", note
        if self.op == "terms":
            vs = ", ".join(f"'{v}'" if isinstance(v, str) else str(v) for v in value)
            return f"{field} in ({vs})", note
        if self.op in ("gte", "lte"):
            op = ">=" if self.op == "gte" else "<="
            v = _abs_since(value) if isinstance(value, str) else value
            return f"{field} {op} '{v}'", note
        if self.op == "match":
            # 目标已改写成 keyword 编码字段时，全文匹配语义退化为精确匹配（等价且更快）
            if note:
                return f"{field} = '{value}'", note
            return f"match({field}, '{value}')", None
        raise ValueError(f"未知过滤 op: {self.op}")

    def _ascii_adapt(self, ascii_safe: bool):
        """非 ASCII 字面量 -> 改写为对编码字段的过滤（详见 ASCII_CODE_FIELDS 的说明）。"""
        if not ascii_safe or not isinstance(self.value, str) or self.value.isascii():
            return self.field, self.value, None
        spec = ASCII_CODE_FIELDS.get(self.field)
        if not spec:
            return self.field, self.value, None
        code_field, mapping = spec
        code = _lookup_code(mapping, self.value)
        if code is None:
            return self.field, self.value, None
        return code_field, code, f"{self.field} → {code_field}（PPL 不支持中文字面量）"


@dataclass
class IRMetric:
    """聚合指标。agg: count(计数，不需要字段) | avg | sum | max | min"""

    name: str
    agg: str
    field: Optional[str] = None

    def to_es(self) -> dict:
        if self.agg == "count":
            # count 由 doc_count（分组）或 hits.total（全局）表达，见 QueryIR.to_es_dsl
            return {}
        return {self.agg: {"field": self.field}}

    def to_ppl(self) -> str:
        if self.agg == "count":
            return f"count() as {self.name}"
        return f"{self.agg}({self.field}) as {self.name}"


@dataclass
class QueryIR:
    """一次查询的中间表示。"""

    index: str
    filters: list[IRFilter] = field(default_factory=list)
    group_by: Optional[str] = None            # 分组字段（terms agg）
    metrics: list[IRMetric] = field(default_factory=list)
    order_by: Optional[str] = None            # 指标名或分组字段
    order_dir: str = "desc"
    limit: int = 20

    # ---------------- 校验（IR 层自检，编译前拦截） ----------------

    def validate(self) -> Optional[str]:
        if not self.index:
            return "IR 缺少 index"
        if not self.group_by and not self.metrics:
            return "IR 缺少聚合指标（既无 metrics 也无 group_by）"
        for f in self.filters:
            if f.op not in ("term", "terms", "gte", "lte", "match"):
                return f"非法过滤 op: {f.op}"
        for m in self.metrics:
            if m.agg not in ("count", "avg", "sum", "max", "min"):
                return f"非法聚合: {m.agg}"
            if m.agg != "count" and not m.field:
                return f"聚合 {m.agg} 需要字段"
        if self.order_dir not in ("asc", "desc"):
            return f"非法排序方向: {self.order_dir}"
        return None

    # ---------------- 编译目标 1：Elasticsearch Query DSL ----------------

    def to_es_dsl(self) -> dict:
        """编译成 ES Query DSL 请求体（_search body）。"""
        err = self.validate()
        if err:
            raise ValueError(f"IR 非法: {err}")
        body: dict = {"size": 0}
        if self.filters:
            body["query"] = {"bool": {"filter": [f.to_es() for f in self.filters]}}
        else:
            body["query"] = {"match_all": {}}

        aggs: dict = {}
        metric_aggs = {m.name: m.to_es() for m in self.metrics if m.agg != "count"}
        if self.group_by:
            inner: dict = dict(metric_aggs)
            terms_spec: dict = {"field": self.group_by, "size": self.limit}
            # 排序：terms agg 默认按 doc_count 降序；显式 order_by 时必须下发 order，
            # 否则 TopN（尤其「最少」的 asc）返回的不是目标桶。
            if self.order_by:
                count_names = {m.name for m in self.metrics if m.agg == "count"}
                # count 指标在分组里由 doc_count 表达，排序键要用 ES 内置的 _count
                order_key = "_count" if self.order_by in count_names else self.order_by
                terms_spec["order"] = {order_key: self.order_dir}
            aggs["group"] = {"terms": terms_spec, "aggs": inner}
            # 分组场景下的 count 用 doc_count 表达，不需要子聚合
        else:
            aggs.update(metric_aggs)
        if aggs:
            body["aggs"] = aggs
        return body

    # ---------------- 编译目标 2：OpenSearch PPL（仅编译，不执行） ----------------

    def to_ppl(self, ascii_safe: bool = True) -> str:
        err = self.validate()
        if err:
            raise ValueError(f"IR 非法: {err}")
        parts = [f"source={self.index}"]
        if self.filters:
            conds = " and ".join(f.to_ppl(ascii_safe=ascii_safe) for f in self.filters)
            parts.append(f"where {conds}")
        stat = ", ".join(m.to_ppl() for m in self.metrics) or "count() as cnt"
        if self.group_by:
            parts.append(f"stats {stat} by {self.group_by}")
        else:
            parts.append(f"stats {stat}")
        if self.order_by:
            arrow = "-" if self.order_dir == "desc" else "+"
            parts.append(f"sort {arrow} {self.order_by}")
        parts.append(f"head {self.limit}")
        return " | ".join(parts)

    def ppl_adaptations(self, ascii_safe: bool = True) -> list[str]:
        """本次 PPL 编译做了哪些方言适配（给用户看，避免"PPL 语句里的字段和问题对不上"的困惑）。"""
        notes: list[str] = []
        if not ascii_safe:
            return notes
        for f in self.filters:
            _, note = f.to_ppl_with_note(ascii_safe=True)
            if note:
                notes.append(note)
        for f in self.filters:
            if f.op in ("gte", "lte") and isinstance(f.value, str):
                if _NOW_REL.match(f.value):
                    notes.append("相对时间 → 绝对时间（PPL 的 where 不支持 ES date math）")
                    break
        return notes


def parse_es_response(body: dict, ir: QueryIR) -> tuple[list[str], list[tuple]]:
    """把 ES 响应解析成 (列名, 行数据)，对齐 DBRunner 的返回契约。

    - 有 group_by：buckets -> (分组字段, *metrics)
    - 无 group_by：单个聚合值
    """
    aggs = body.get("aggregations") or {}
    metric_names = [m.name for m in ir.metrics]

    if ir.group_by:
        cols = [ir.group_by, *metric_names]
        bucket = aggs.get("group") or {}
        rows: list[tuple] = []
        for b in bucket.get("buckets", []):
            row: list = [b.get("key")]
            for m in ir.metrics:
                if m.agg == "count":
                    row.append(b.get("doc_count"))
                else:
                    sub = b.get(m.name) or {}
                    row.append(sub.get("value"))
            rows.append(tuple(row))
        return cols, rows

    cols = list(metric_names) or ["value"]
    row: list = []
    for m in ir.metrics:
        if m.agg == "count":
            row.append((body.get("hits") or {}).get("total", {}).get("value"))
        else:
            sub = aggs.get(m.name) or {}
            row.append(sub.get("value"))
    return cols, [tuple(row)]
