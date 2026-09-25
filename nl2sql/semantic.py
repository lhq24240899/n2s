"""语义层：计量检测智能问数的核心差异化模块（与具体行业无关，可复用）。

为什么通用 Text-to-SQL 不够？
计量检测行业真正难的不是「自然语言→SQL」，而是「业务语言→数据语义」：
- 同一句话在不同实验室口径不同（"检测准时率" 华东和华南算法可能不一样）；
- 专业术语 LLM 听不懂（"EMC" 指电磁兼容检测，"软测" 指软件测评）；
- 指标必须绑定 SQL 表达式，否则 LLM 自己猜口径必然出错。

语义层就是 LLM 与业务之间的「翻译层」，承载行业知识。本模块提供：
- Metric        ：指标定义（绑定口径 + SQL 提示 + 可切片维度 + 别名）
- SynonymMap    ：同义词 → 标准业务术语（支持歧义澄清）
- KnowledgeGraph：核心实体关系，供 Schema Linking 增强
- SemanticLayer ：聚合上述三者的语义中台
- SemanticMapper：把用户问题映射为 MappedQuery（归一化问题 + 解析出的指标/实体）

设计原则：语义层只做「确定性映射」，不做「猜测」。解析不到就要求澄清，
绝不让 LLM 在毫无业务约束下自由发挥（这是防幻觉的第一道闸）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .glossary import Glossary, GlossaryEntry, Metric as GlossaryMetric


class MetricLevel:
    GROUP = "集团级"
    OPERATION = "运营级"
    QUALITY = "质量级"


@dataclass
class Metric:
    """指标定义。definition 是给人/LLM 的统一口径；sql_hint 是参考 SQL 片段。"""

    id: str
    name: str
    level: str  # MetricLevel.*
    domain: str  # 业务板块或 "通用"
    definition: str  # 统一口径（给人看，也喂给 LLM）
    sql_hint: str  # 参考 SQL 表达式 / 模板
    source_tables: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)  # 可切片维度：区域/实验室/业务线/时间
    aliases: list[str] = field(default_factory=list)  # 用户可能说的其它叫法


@dataclass
class Synonym:
    """一条同义词。ambiguous=True 表示需要向用户澄清（如"那个做环境的"）。"""

    phrase: str  # 用户原话，如 "EMC"
    canonical: str  # 归一化标准术语，如 "电磁兼容检测"
    target: str = "business_line"  # business_line/region/metric/time/...
    value: str = ""  # 归一化值（如 "emc"），缺省用 canonical
    ambiguous: bool = False
    clarification: str = ""  # 歧义时向用户确认的话


class SynonymMap:
    def __init__(self, synonyms: list[Synonym]):
        self._by_phrase = {s.phrase: s for s in synonyms}

    def resolve(self, text: str) -> list[Synonym]:
        """返回所有出现在 text 中的同义词，按短语长度降序（避免子串误匹配）。"""
        hits: list[Synonym] = []
        for phrase in sorted(self._by_phrase, key=len, reverse=True):
            if phrase in text:
                hits.append(self._by_phrase[phrase])
        return hits


@dataclass
class KGNode:
    kind: str  # lab / region / business_line / standard / ...
    name: str
    attrs: dict = field(default_factory=dict)


@dataclass
class KGEdge:
    src: str
    rel: str
    dst: str


class KnowledgeGraph:
    """轻量业务知识图谱：实体 + 关系，用于增强 Schema Linking。"""

    # 计量检测涉及的物理表名，linker 据此把实体关联到具体表
    TABLE_KINDS = {
        "reports",
        "labs",
        "business_lines",
        "contracts",
        "equipment",
        "test_records",
        "trust_orders",
        "customers",
        "samples",
    }

    def __init__(
        self,
        nodes: list[KGNode] | None = None,
        edges: list[KGEdge] | None = None,
    ):
        self.nodes = {n.name: n for n in (nodes or [])}
        self.edges = edges or []

    def neighbors(self, name: str) -> list[KGEdge]:
        return [e for e in self.edges if e.src == name]

    def related_tables(self, name: str) -> list[str]:
        """给定实体名，返回知识图谱中关联的表（供 linker 增强）。"""
        out: list[str] = []
        for e in self.edges:
            if e.src == name and e.dst in self.TABLE_KINDS:
                out.append(e.dst)
        return out


@dataclass
class MappedQuery:
    """语义映射的产出：归一化问题 + 解析出的指标/实体 + 可解释 reasons + 澄清。"""

    original: str
    normalized: str  # 同义词展开后的问题（喂给检索与 LLM）
    metric: Optional[Metric]
    entities: dict  # region/business_line/time/...
    resolved_synonyms: list[str]
    clarification: Optional[str]  # 若需澄清则非空
    reasons: list[str]
    # 触发澄清的歧义同义词；上层据此在用户确认后「回到原问题」重跑
    ambiguous_synonyms: list[Synonym] = field(default_factory=list)


class SemanticLayer:
    """语义中台：聚合指标、同义词、知识图谱，并提供「按指标生成口径 Glossary」。"""

    # 实体键 -> 数据库列；用于把「用户口语」转成「必须落到 SQL 的规范过滤条件」
    DEFAULT_ENTITY_COLUMNS = {
        "region": "labs.region",
        "business_line": "business_lines.code",
    }

    def __init__(
        self,
        metrics: list[Metric],
        synonyms: list[Synonym],
        graph: KnowledgeGraph,
        base_entries: list[GlossaryEntry] | None = None,
        entity_columns: dict[str, str] | None = None,
    ):
        self.metrics = {m.id: m for m in metrics}
        self.synonyms = SynonymMap(synonyms)
        self.graph = graph
        self.base_entries = base_entries or []
        self.entity_columns = entity_columns or dict(self.DEFAULT_ENTITY_COLUMNS)

    def get_metric(self, mid: str) -> Optional[Metric]:
        return self.metrics.get(mid)

    def glossary_for(
        self,
        metric_id: Optional[str] = None,
        entities: Optional[dict] = None,
    ) -> Glossary:
        """把指定指标（及基础术语 + 已识别实体）渲染成 Glossary，注入 prompt 作为业务约束。

        entities 里的区域/业务线会被转成**硬过滤条件**下发，避免 LLM 把用户口语
        （如"华南区"）当成数据库取值写进 WHERE，导致匹配 0 行、结果为 NULL。
        """
        ms: list[GlossaryMetric] = []
        if metric_id and metric_id in self.metrics:
            m = self.metrics[metric_id]
            ms.append(
                GlossaryMetric(
                    m.name,
                    f"{m.definition}  [SQL参考: {m.sql_hint}]",
                )
            )

        entries = list(self.base_entries)
        if entities:
            entries.extend(self._entity_entries(entities))
        return Glossary(metrics=ms, entries=entries)

    def _entity_entries(self, entities: dict) -> list[GlossaryEntry]:
        """把已识别实体渲染成「必须使用的规范过滤条件」。"""
        notes = {
            "region": "用户口语可能带'区'字（如'华南区'），但库内规范值不带，务必用此值",
            "business_line": "这是业务线 code，不要用中文名",
        }
        out: list[GlossaryEntry] = []
        for key, column in self.entity_columns.items():
            value = entities.get(key)
            if not value:
                continue
            out.append(
                GlossaryEntry(
                    f"本次过滤-{key}",
                    f"必须使用 {column} = '{value}'"
                    f"（{notes.get(key, '务必使用该规范值')}；"
                    f"禁止写成 '{value}区' 或用户原话）",
                )
            )
        return out


class SemanticMapper:
    """把用户问题映射为 MappedQuery。只做确定性映射，绝不猜测。"""

    REGIONS = ["华东", "华南", "华北", "华中", "西南", "西北", "东北"]

    def __init__(self, layer: SemanticLayer):
        self.layer = layer

    def map(self, question: str) -> MappedQuery:
        reasons: list[str] = []
        syns = self.layer.synonyms.resolve(question)
        normalized = question
        entities: dict = {}
        ambiguous: list[Synonym] = []
        resolved: list[str] = []

        # 1) 同义词展开
        for s in syns:
            if s.ambiguous:
                ambiguous.append(s)
                reasons.append(f"歧义同义词:{s.phrase} (需澄清)")
                continue
            if s.canonical not in normalized:
                normalized = f"{normalized} {s.canonical}"
            if s.target in ("business_line", "region", "metric", "time"):
                entities[s.target] = s.value or s.canonical
            resolved.append(f"{s.phrase}->{s.canonical}")
            reasons.append(f"同义词:{s.phrase}->{s.canonical}")

        # 2) 指标解析（问题包含指标名或别名）
        metric: Optional[Metric] = None
        for m in self.layer.metrics.values():
            if m.name in question or any(a in question for a in m.aliases):
                metric = m
                entities.setdefault("metric", m.id)
                reasons.append(f"指标命中:{m.name}")
                break

        # 3) 区域实体抽取
        for r in self.REGIONS:
            if r in question:
                entities["region"] = r
                reasons.append(f"区域实体:{r}")
                # 归一化口语写法："华南区" -> "华南"，避免 LLM 把带"区"的原话写进 WHERE
                if f"{r}区" in normalized:
                    normalized = normalized.replace(f"{r}区", r)
                    reasons.append(f"区域归一化:{r}区->{r}")

        # 4) 时间实体（粗粒度）
        if "上个月" in question:
            entities["time"] = "上个月"
        elif "上周" in question:
            entities["time"] = "上周"
        elif "本月" in question:
            entities["time"] = "本月"
        if "time" in entities:
            reasons.append(f"时间实体:{entities['time']}")

        # 5) 歧义优先：需澄清则直接返回，不继续生成
        if ambiguous:
            clar = "；".join(a.clarification for a in ambiguous)
            return MappedQuery(
                original=question,
                normalized=normalized,
                metric=metric,
                entities=entities,
                resolved_synonyms=resolved,
                clarification=clar,
                reasons=reasons,
                ambiguous_synonyms=ambiguous,
            )

        return MappedQuery(
            original=question,
            normalized=normalized,
            metric=metric,
            entities=entities,
            resolved_synonyms=resolved,
            clarification=None,
            reasons=reasons,
        )
