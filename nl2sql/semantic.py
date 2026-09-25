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

    # 分组维度 -> 用于 GROUP BY 的展示列（问「各业务线」要每行一个业务线）
    GROUP_COLUMNS = {
        "business_line": "business_lines.name",
        "lab": "labs.name",
        "region": "labs.region",
        "customer": "customers.name",
    }

    # 时间口径：口语 -> 规范 SQL 窗口。不固定死它，LLM 会时而写滚动窗口时而写自然月，
    # 同一个"上个月"返回不同数字（评估集实测抓到过）。
    TIME_WINDOWS = {
        "上个月": "r.issued_at >= CURRENT_DATE - INTERVAL '30 days'（滚动30天，**不要**用自然月 date_trunc）",
        "上月": "r.issued_at >= CURRENT_DATE - INTERVAL '30 days'（滚动30天，**不要**用自然月 date_trunc）",
        "上周": "r.issued_at >= CURRENT_DATE - INTERVAL '7 days'（滚动7天）",
        "本月": "r.issued_at >= date_trunc('month', CURRENT_DATE)（本月1号至今）",
        "这个月": "r.issued_at >= date_trunc('month', CURRENT_DATE)（本月1号至今）",
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
            # 分组/排名问句下不下发标量 SQL 模板——它会与「GROUP BY / 返回名称列」的
            # 约束打架（评估集实测：hint 是标量 SUM 模板时，LLM 会无视分组要求）
            shaped = bool(entities and (entities.get("group_by") or entities.get("topn")))
            hint = "" if shaped else f"  [SQL参考: {m.sql_hint}]"
            ms.append(GlossaryMetric(m.name, f"{m.definition}{hint}"))

        entries = list(self.base_entries)
        if entities:
            entries.extend(self._entity_entries(entities))
        return Glossary(metrics=ms, entries=entries)

    def _entity_entries(self, entities: dict) -> list[GlossaryEntry]:
        """把已识别实体渲染成「必须使用的规范过滤条件」，并下发分组维度。"""
        notes = {
            "region": "用户口语可能带'区'字（如'华南区'），但库内规范值不带，务必用此值",
            "business_line": "这是业务线 code，不要用中文名",
        }
        out: list[GlossaryEntry] = []

        # 分组维度优先：要求 GROUP BY 该维度，每行一个取值；同维度不能再加过滤
        group_by = entities.get("group_by")
        group_col = self.GROUP_COLUMNS.get(group_by) if group_by else None
        if group_col:
            out.append(
                GlossaryEntry(
                    "分组维度（重要）",
                    f"本次问的是「各{group_by}」的分布，必须按 {group_col} 做 GROUP BY，"
                    f"**每个取值输出一行**（不要把全部数据聚合成一个数），"
                    f"**SELECT 里必须带 {group_col} 这个名称列**（不要用 id 列当分组标识），"
                    f"并且不要再对该维度添加 WHERE 过滤条件",
                )
            )

        # 总量问句：明确要求 COUNT，防止 LLM 自作主张换聚合
        if entities.get("count"):
            out.append(
                GlossaryEntry(
                    "本次聚合（重要）",
                    "这是一个**计数**问题：用 COUNT(*) 统计行数，不要对其它列做 SUM/AVG",
                )
            )

        # 最值/排名问句：要求返回"是哪个"的名称列，而不是只给一个数
        if entities.get("topn"):
            out.append(
                GlossaryEntry(
                    "排名问句（重要）",
                    "这是一个**最值/排名**问题：用 ORDER BY <聚合值> ASC/DESC 加 LIMIT 返回最值那一行；"
                    "**第一列必须是名称标识列**（如实验室名/业务线名/客户名），不要只返回聚合数值，也不要用 id 列",
                )
            )

        # 时间口径：把口语时间钉死成规范 SQL 窗口（评估集实测：不钉死会漂移）
        time_word = entities.get("time")
        if time_word:
            window = self.TIME_WINDOWS.get(time_word)
            if window:
                out.append(
                    GlossaryEntry(
                        "本次时间口径（重要）",
                        f"问题里的「{time_word}」必须翻译成：{window}",
                    )
                )

        for key, column in self.entity_columns.items():
            if group_col and key == group_by:
                continue  # 正在分组的维度不能再当过滤条件
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

    # 「各X / 按X / 每个X」= 按 X 分组（GROUP BY），不是过滤条件。
    # 若不区分，LLM 会把"各业务线"当成普通问句，再叠加继承来的业务线过滤，
    # 结果只剩一个数值（实测踩过）。键与 SemanticLayer.GROUP_COLUMNS 对齐。
    GROUP_KEYWORDS = {
        "business_line": ("业务线", "业务板块"),
        "lab": ("实验室",),
        "region": ("区域", "大区", "地区"),
        "customer": ("客户",),
    }
    # "所有"**不是**分组触发词：它是全称限定，语义是"合起来一共多少"
    # （评估集实测：「所有实验室的设备总数是多少」被误判成按实验室分组，返回 5 行而非总数）。
    GROUP_TRIGGERS = ("各", "各个", "每个", "每", "按", "分别", "不同")

    # 总量问句：「多少台 / 多少个 / 总数」这类**计数意图**。
    # 这些问题没有注册指标，若不显式识别会被当成文档问答（评估集实测：总量类 0/8 全走错路）。
    COUNT_PATTERNS = (
        "多少台", "多少个", "多少份", "多少条", "多少家", "多少张",
        "总数", "数量是多少", "有几个", "几台", "几个",
    )

    # 最值/排名问句：「最高的 / 最多的 / 最低的」。
    # 不识别的话部分问句会被当成文档问答；且 LLM 常只返回聚合数值，
    # 忘了返回"是哪个"的名称列（评估集实测）。
    TOPN_PATTERNS = ("最高", "最多", "最低", "最少", "排名", "前三个", "前三名", "top")

    def __init__(self, layer: SemanticLayer):
        self.layer = layer

    def _detect_group_by(self, question: str) -> Optional[str]:
        """识别「各业务线 / 各实验室 / 各区域」这类分组维度。"""
        if not any(t in question for t in self.GROUP_TRIGGERS):
            return None
        for dim, keywords in self.GROUP_KEYWORDS.items():
            if any(k in question for k in keywords):
                return dim
        return None

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

        # 2.5) 总量问句：没有注册指标的计数意图 -> 显式标记，保证走结构化 SQL
        #      （不标记会被当成文档问答：评估集实测「总量」类 0/8 全部走错路）
        if not metric and any(p in question for p in self.COUNT_PATTERNS):
            entities["count"] = True
            reasons.append("总量问句: COUNT 计数（走结构化查询）")

        # 2.6) 最值/排名问句：同样需要显式路由 + 约束返回形态
        ql = question.lower()
        if any(p in question for p in self.TOPN_PATTERNS) or "top" in ql:
            entities["topn"] = True
            reasons.append("排名问句: 返回最值所在行（ORDER BY + LIMIT），需含名称列")

        # 3) 区域实体抽取
        for r in self.REGIONS:
            if r in question:
                entities["region"] = r
                reasons.append(f"区域实体:{r}")
                # 归一化口语写法："华南区" -> "华南"，避免 LLM 把带"区"的原话写进 WHERE
                if f"{r}区" in normalized:
                    normalized = normalized.replace(f"{r}区", r)
                    reasons.append(f"区域归一化:{r}区->{r}")

        # 3.5) 分组维度：「各业务线 / 各实验室 / 各区域」-> 要求按该维度 GROUP BY
        group_by = self._detect_group_by(question)
        if group_by:
            entities["group_by"] = group_by
            reasons.append(f"分组维度:{group_by}（要求按该维度分组，每行一个取值）")

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
