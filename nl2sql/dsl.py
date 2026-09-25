"""QueryIR：方言无关的查询中间表示 —— 一份 IR，编译到多种执行语言。

为什么要有 IR（对应 JD「自然语言转 SQL/PPL/DSL」的正确姿势）：
- 让 LLM 直接写 PPL/JSON = 每种语言各赌一次格式化（括号、保留字、字段名全靠模型）；
- 有了 IR，LLM/规则只负责**填槽**，各执行语言的语法正确性由**编译器**保证；
- 校验、权限、审计都能作用在 IR 上 —— 换执行引擎不换安全等级。

当前提供两个编译目标：
- `to_es_dsl()`   → Elasticsearch Query DSL（真集群执行：阿里云 ES）
- `to_ppl()`      → OpenSearch PPL（**仅编译产物**；PPL 是 OpenSearch 的语言，
  阿里云 ES 不能执行 —— 诚实标注"需 OpenSearch"，不假装能跑）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


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

    def to_ppl(self) -> str:
        if self.op == "term":
            v = f"'{self.value}'" if isinstance(self.value, str) else self.value
            return f"{self.field} = {v}"
        if self.op == "terms":
            vs = ", ".join(f"'{v}'" if isinstance(v, str) else str(v) for v in self.value)
            return f"{self.field} in ({vs})"
        if self.op in ("gte", "lte"):
            return f"{self.field} {'>=' if self.op == 'gte' else '<='} {self.value}"
        if self.op == "match":
            return f"match({self.field}, '{self.value}')"
        raise ValueError(f"未知过滤 op: {self.op}")


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

    def to_ppl(self) -> str:
        err = self.validate()
        if err:
            raise ValueError(f"IR 非法: {err}")
        parts = [f"source={self.index}"]
        if self.filters:
            conds = " and ".join(f.to_ppl() for f in self.filters)
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
