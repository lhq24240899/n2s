"""多轮对话上下文：维护上一轮的查询状态，使追问能继承维度。

典型场景：
  用户：「华东区上个月可靠性试验准时完成率是多少」
  用户：「那华南区呢？」
系统应继承 {业务线=可靠性, 指标=准时完成率, 时间=上个月}，
仅把 region 替换为 华南 —— 而不是让用户把整句话再说一遍。

实现要点：
- QueryContext 保存最近一次成功查询的维度（metric/business_line/region/time）。
- inherit() 把上一轮上下文回填到本轮「缺失」的实体，并重新拼出归一化问题，
  保证继承的维度能进入检索打分与 LLM 提示词。
- 只有用户「显式说了」的维度才会覆盖上下文（如"华南区"覆盖 region），
  未提及的维度沿用上轮。这就是多轮"只改一点"的体验来源。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .semantic import Metric, MappedQuery


@dataclass
class QueryContext:
    metric: Optional[Metric] = None
    business_line: Optional[str] = None  # 业务线 code，如 "reliability"
    region: Optional[str] = None
    customer: Optional[str] = None       # 客户名（语义层登记的取值），如 "某汽车客户"
    time: Optional[str] = None
    dimensions: list[str] = field(default_factory=list)
    # 上一轮的**原始问题**与**结果摘要**。它们不参与槽位继承，只喂给「追问改写」
    # （nl2sql/condense.py）：指代型追问（"金额是多少"）的锚点在结果行里，
    # 槽位继承给不出来 —— 有这两项，改写器才能把锚点补回问句。
    last_question: str = ""
    last_result: str = ""

    def inherit(self, mapped: MappedQuery, *, inherit_filters: bool = True) -> MappedQuery:
        """把上一轮上下文回填到本轮缺失的实体，返回补全后的 MappedQuery。

        inherit_filters=False：**只继承指标**，不继承 region/business_line/customer/time
        这些过滤维度。这是给「空结果回退」用的（见 GRGQueryEngine._answer）：若继承来的
        过滤条件把结果筛空了，可以忽略它们重查一次——指标这类"问题类型"信息仍然沿用，
        语义不会跑偏。
        """
        ents = dict(mapped.entities)
        metric = mapped.metric or self.metric

        # 本轮要「分组」的维度，不能再从上下文继承成过滤条件。
        # 例：上一轮问「华东区可靠性…」，本轮问「各业务线的准时率」，
        # 若沿用 business_line=reliability 就只剩一个数值（实测 bug）。
        group_by = ents.get("group_by")
        skip = {group_by} if group_by else set() if inherit_filters else None

        inherited_keys: list[str] = []
        if self.metric is not None and "metric" not in ents:
            ents["metric"] = self.metric.id
            inherited_keys.append("metric")
        if inherit_filters:
            if self.business_line is not None and "business_line" not in ents and "business_line" not in skip:
                ents["business_line"] = self.business_line
                inherited_keys.append("business_line")
            if self.region is not None and "region" not in ents and "region" not in skip:
                ents["region"] = self.region
                inherited_keys.append("region")
            if self.customer is not None and "customer" not in ents and "customer" not in skip:
                ents["customer"] = self.customer
                inherited_keys.append("customer")
            if self.time is not None and "time" not in ents and "time" not in skip:
                ents["time"] = self.time
                inherited_keys.append("time")

        # 重新拼出归一化问题，让继承的维度进入检索/生成
        extra: list[str] = []
        if "region" in ents and ents["region"] not in mapped.normalized:
            extra.append(ents["region"])
        if "business_line" in ents and ents["business_line"] not in mapped.normalized:
            extra.append(ents["business_line"])
        if "customer" in ents and ents["customer"] not in mapped.normalized:
            extra.append(ents["customer"])
        normalized = mapped.normalized
        if extra:
            normalized = f"{normalized} {' '.join(extra)}"

        reasons = list(mapped.reasons)
        if not inherit_filters:
            reasons.append("空结果回退: 不使用上一轮继承的过滤维度（区域/业务线/客户/时间）")
        elif inherited_keys:
            reasons.append(f"上下文继承: 补齐缺失维度 {inherited_keys}")
        if group_by:
            reasons.append(f"分组优先: 已忽略继承的「{group_by}」过滤（本轮要按它分组）")

        return MappedQuery(
            original=mapped.original,
            normalized=normalized,
            metric=metric,
            entities=ents,
            resolved_synonyms=mapped.resolved_synonyms,
            clarification=mapped.clarification,
            reasons=reasons,
            ambiguous_synonyms=mapped.ambiguous_synonyms,
            inherited_dimensions=inherited_keys,
        )

    def update_from(self, mapped: MappedQuery) -> None:
        """用本轮解析出的实体更新上下文，供下一轮继承。"""
        e = mapped.entities
        if mapped.metric is not None:
            self.metric = mapped.metric
        if "business_line" in e:
            self.business_line = e["business_line"]
        if "region" in e:
            self.region = e["region"]
        if "customer" in e:
            self.customer = e["customer"]
        if "time" in e:
            self.time = e["time"]
        # 记住原始问题（不含继承补的维度），供下一轮的「追问改写」用。
        # 存 original 而不是 normalized：改写器要看到**用户真正说的那句**，
        # 否则上一轮补齐的维度会被当成用户说的，指代对象反而更难判断。
        self.last_question = mapped.original or self.last_question

    def note_result(self, summary: str) -> None:
        """记住上一轮结果的摘要（如 "某汽车客户" / "0.9167"），供追问改写定位指代对象。"""
        self.last_result = (summary or "")[:200]

    def reset(self) -> None:
        self.metric = None
        self.business_line = None
        self.region = None
        self.customer = None
        self.time = None
        self.dimensions = []
        self.last_question = ""
        self.last_result = ""
