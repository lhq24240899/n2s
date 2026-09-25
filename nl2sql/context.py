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
    time: Optional[str] = None
    dimensions: list[str] = field(default_factory=list)

    def inherit(self, mapped: MappedQuery) -> MappedQuery:
        """把上一轮上下文回填到本轮缺失的实体，返回补全后的 MappedQuery。"""
        ents = dict(mapped.entities)
        metric = mapped.metric or self.metric

        if self.metric is not None and "metric" not in ents:
            ents["metric"] = self.metric.id
        if self.business_line is not None and "business_line" not in ents:
            ents["business_line"] = self.business_line
        if self.region is not None and "region" not in ents:
            ents["region"] = self.region
        if self.time is not None and "time" not in ents:
            ents["time"] = self.time

        inherited = (
            (self.metric is not None and "metric" not in mapped.entities)
            or (self.business_line is not None and "business_line" not in mapped.entities)
            or (self.region is not None and "region" not in mapped.entities)
            or (self.time is not None and "time" not in mapped.entities)
        )

        # 重新拼出归一化问题，让继承的维度进入检索/生成
        extra: list[str] = []
        if "region" in ents and ents["region"] not in mapped.normalized:
            extra.append(ents["region"])
        if "business_line" in ents and ents["business_line"] not in mapped.normalized:
            extra.append(ents["business_line"])
        normalized = mapped.normalized
        if extra:
            normalized = f"{normalized} {' '.join(extra)}"

        reasons = mapped.reasons
        if inherited:
            reasons = reasons + ["上下文继承: 补齐缺失维度"]

        return MappedQuery(
            original=mapped.original,
            normalized=normalized,
            metric=metric,
            entities=ents,
            resolved_synonyms=mapped.resolved_synonyms,
            clarification=mapped.clarification,
            reasons=reasons,
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
        if "time" in e:
            self.time = e["time"]

    def reset(self) -> None:
        self.metric = None
        self.business_line = None
        self.region = None
        self.time = None
        self.dimensions = []
