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

import re
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
class MetricGuard:
    """指标的**口径硬约束**（数据，不是代码里的 if）—— 给出口的「口径一致性校验」用。

    为什么写成数据：口径会变、指标会加，把断言和指标定义放一起，改口径时一眼能看到
    要同步改什么。判定逻辑在 `nl2sql/caliber.py`（纯函数、可离线测）。

    - trigger：问题里出现这些词才启用 **must_filter** 断言（如 `("已开票",)`）。空 = 每次都判。
      （`forbid_tables` 不受它门控——"该口径不得 JOIN 某表"是定义的一部分，与问法无关。）
    - must_filter：必须出现的等值过滤，`{"contracts.settled_status": ("已开票",)}`。
    - forbid_tables：该口径下明令不得 JOIN 的表（一对多连接会把金额按行重复累加）。
    """

    trigger: tuple[str, ...] = ()
    must_filter: dict[str, tuple[str, ...]] = field(default_factory=dict)
    forbid_tables: tuple[str, ...] = ()


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
    # 本轮**从上一轮继承**来的维度键（region/time/business_line/metric）。
    # 用途：结果为空时上层可以只在"继承来的"维度上放宽重查（用户本轮明说的条件绝不动），
    # 也能在界面上如实区分"你这次说的"和"沿用上一轮的"。
    inherited_dimensions: list[str] = field(default_factory=list)


class SemanticLayer:
    """语义中台：聚合指标、同义词、知识图谱，并提供「按指标生成口径 Glossary」。"""

    # 实体键 -> 数据库列；用于把「用户口语」转成「必须落到 SQL 的规范过滤条件」
    DEFAULT_ENTITY_COLUMNS = {
        "region": "labs.region",
        "business_line": "business_lines.code",
        # 客户：值来自 customers.name。登记在这里，Glossary 才会把
        # 「必须使用 customers.name = '某汽车客户'」当**硬过滤**下发。
        "customer": "customers.name",
    }

    # 分组维度 -> 用于 GROUP BY 的展示列（问「各业务线」要每行一个业务线）
    GROUP_COLUMNS = {
        "business_line": "business_lines.name",
        "lab": "labs.name",
        "region": "labs.region",
        "customer": "customers.name",
    }

    # 分组展示列的**可接受写法**：同一维度在不同主题域可能落到不同物理列。
    # 实测（出口展示列校验的误报复核）：G01「最近一个季度各业务板块的营收是多少」
    # 取自 `business_segment_revenue`，那个域里"业务板块"是 `business_segment` 列，
    # 不是 `business_lines.name`——只认一个列名就会对这条**完全正确**的 SQL 误报。
    GROUP_COLUMN_ACCEPT = {
        "business_line": ("business_segment",),
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
        entity_values: dict[str, tuple[str, ...]] | None = None,
        metric_guards: dict[str, MetricGuard] | None = None,
    ):
        self.metrics = {m.id: m for m in metrics}
        self.synonyms = SynonymMap(synonyms)
        self.graph = graph
        self.base_entries = base_entries or []
        self.entity_columns = entity_columns or dict(self.DEFAULT_ENTITY_COLUMNS)
        # 实体**取值**登记表：键 -> 库里真实存在的取值（如 customer -> 客户名列表）。
        # 为什么必须登记：区域是固定枚举（SemanticMapper.REGIONS 可硬编码），
        # 但客户名属于**业务数据**，只能在装配时由调用方注入（示例 schema 从表结构的
        # sample_values 取）。不登记的话，问句里写清了客户名也抽不出来，
        # 会被当成"全公司"来算（真机踩过：「某汽车客户的合同金额是多少」返回全库 5,450,000）。
        self.entity_values = entity_values or {}
        # 指标口径的硬约束（给出口的口径一致性校验用，见 nl2sql/caliber.py）。
        # 空 = 不做指标级断言；单条最多漏一个，判错也只标注不改写。
        self.metric_guards = metric_guards or {}

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
            "customer": "这是客户名；contracts 表没有 name 列，必须 JOIN customers 才能过滤/展示",
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
            # 「禁止写成 'X区'」这半句只对**区域**成立（库内取值不带"区"）。
            # 其它维度保持原样拼装，避免改动既有区域/业务线的下发文本。
            suffix = "" if key == "customer" else f"；禁止写成 '{value}区' 或用户原话"
            out.append(
                GlossaryEntry(
                    f"本次过滤-{key}",
                    f"必须使用 {column} = '{value}'"
                    f"（{notes.get(key, '务必使用该规范值')}{suffix}）",
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
    # 「X客户的」= 客户限定语（过滤意图），用于识别"写了客户名但我们没登记"的情况。
    # 刻意只认「…客户的」这个形态：排名句（"最高的客户是哪个"）不含它，不会被误伤。
    _CUSTOMER_FILTER_RE = re.compile(r"([\u4e00-\u9fa5]{1,8}客户)的")
    # 客户限定语上要剥掉的泛指前缀（分组/全称/指代）：剥完只剩"客户"即为泛指，不是具体客户名。
    _CUSTOMER_PREFIX_RE = re.compile(
        r"^(?:各|每个|各个|每|所有|全部|全量|不同|哪些|哪个|哪家|多少|几家|这些|那些|该|本)+"
    )
    # 机构/基地全称模式：命中即视为长实体，优先于词级同义词（见 map() 第 0 步）
    _FULL_NAME_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z（）()]{2,}有限公司")

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

    # 知识型问句（定义 / 解释 / 对照）：要查**知识库**，不是查数据。
    # 与 count/topn 方向相反：那两个是"没有指标也要走结构化"，
    # 这个是"**有指标也要走文档问答**"。
    #
    # 真机踩过：先问「华南区上个月…准时率」，再问「EMC 是什么意思」——
    # 上一轮的指标被 inherit() 回填进 merged，路由据 merged.metric 判成结构化问句，
    # 于是拿"准时率 WHERE 业务线=emc AND 区域=华南"去查库，返回「无匹配数据」。
    # 例问「ISO/IEC 17025 和 GB/T 27025 有什么区别」同样中招。
    KNOWLEDGE_PATTERNS = (
        "是什么", "什么是", "什么意思", "含义", "定义", "解释", "介绍",
        "有什么区别", "区别",
    )

    # 「疑似区域」：华X / X华，允许带"区"后缀（华西、华西区、华东区…）。
    # 只覆盖"华"字系写法——不能放宽成 [东南西北]{2}，否则「这个东西」「东西南北」
    # 会被误判成区域。
    _REGION_CANDIDATE_RE = re.compile(r"(?:华[东南西北中]|[东南西北中]华)区?")

    # 「疑似指标」：以"率"结尾的词（本项目所有比率型指标都以"率"结尾）。
    _RATE_WORD_RE = re.compile(r"[\u4e00-\u9fa5]{2,4}率")

    # 正则可能把虚词前缀一起吃进来（「那准确率」）——提示语里要还原成「准确率」
    _PREFIX_STOPWORDS_RE = re.compile(r"^[那的这那个和与及也还]+")

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
        entities: dict = {}

        # 0) 长实体优先：公司/基地**全称**先匹配并从问题中屏蔽，再做词级同义词展开。
        #    否则「广电计量检测（上海）有限公司的设备利用率」里的"计量"会被当成
        #    "计量服务"业务线 -> 生成 WHERE b.code='calibration' 这种问句里没有的过滤
        #    （评估集 B02 实测：本应 0.88，却返回被污染后的 0.81）。
        masked = question
        m = self._FULL_NAME_RE.search(question)
        if m:
            full = m.group(0)
            masked = question.replace(full, " ")
            entities["lab"] = full
            reasons.append(f"长实体(机构全称):{full}")

        syns = self.layer.synonyms.resolve(masked)
        # normalized 保留**原问题**：全称要留给 LLM 做 l.name 过滤，
        # 屏蔽只作用于同义词展开（否则模型拿不到"上海"这个条件，会干脆不加过滤）。
        normalized = question
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

        # 2.7) 知识型问句（定义/解释/对照）：显式标记，由引擎优先路由到文档问答。
        #      注意这里**不判 metric**——正是"即使解析出指标也要走 RAG"。
        #      该标记不会被 inherit() 回填（它只回填 metric/region/business_line/time），
        #      所以不会跨轮污染。
        if any(p in question for p in self.KNOWLEDGE_PATTERNS):
            entities["knowledge"] = True
            reasons.append("知识型问句: 走文档问答（定义/解释/对照）")

        # 3) 区域实体抽取
        for r in self.REGIONS:
            if r in question:
                entities["region"] = r
                reasons.append(f"区域实体:{r}")
                # 归一化口语写法："华南区" -> "华南"，避免 LLM 把带"区"的原话写进 WHERE
                if f"{r}区" in normalized:
                    normalized = normalized.replace(f"{r}区", r)
                    reasons.append(f"区域归一化:{r}区->{r}")

        # 3.1) 业务取值抽取（客户名等）：值必须**登记在语义层**（layer.entity_values）。
        #      区域能硬编码成枚举，客户名是业务数据，只能由装配方注入。
        #      真机踩过：不登记时「某汽车客户的合同金额是多少」会把客户名整个丢掉，
        #      SQL 退化成 SELECT SUM(amount) FROM contracts（全库）→ 5,450,000（正确 2,540,000）。
        for key, values in (self.layer.entity_values or {}).items():
            if key in entities:
                continue
            for v in sorted(values, key=len, reverse=True):   # 长值优先，避免前缀误配
                if v and v in question:
                    entities[key] = v
                    reasons.append(f"{key}实体:{v}")
                    break

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

        # 6) 未识别槽位自检：本轮提到了"疑似区域/指标"的词却没识别出来 -> 澄清
        clar = self._unrecognized_slot_clarification(question, entities, metric)
        if clar:
            reasons.append("未识别槽位: 改为澄清（不静默沿用上一轮的值）")

        return MappedQuery(
            original=question,
            normalized=normalized,
            metric=metric,
            entities=entities,
            resolved_synonyms=resolved,
            clarification=clar,
            reasons=reasons,
        )

    def _unrecognized_slot_clarification(
        self, question: str, entities: dict, metric: Optional[Metric]
    ) -> Optional[str]:
        """问句里出现了「疑似区域 / 疑似指标」的词，但我们没能识别出来 -> 澄清。

        为什么需要这一步？
        `QueryContext.inherit()` 只补「**缺失**」的槽位，而在它眼里
        「用户没提」和「用户提了但我们没懂」**是同一种情况** —— 于是解析失败就等于"缺失"，
        会被上一轮的值静默填上。

        真机踩过：先问「华南呢」（返回 0.8667），再问「那华西 准确率呢」——
        「华西」不在区域词表、「准确率」不是任何指标的别名，两个槽位都空了，
        于是双双沿用上一轮（区域仍是华南、指标仍是准时率）。用户拿到一个
        "看起来很正常、但问的根本不是他要的"数字。**拿错范围的数据，比说"我没懂"危险得多。**

        原则：**只继承用户没提到的；用户提到了、我们没懂，就问。**
        """
        hints: list[str] = []

        # ① 疑似区域但不在词表（"华西"）。
        #    仅在**本轮没解析出有效区域**时提示：否则澄清后用户回一句「华南的准时率」，
        #    重跑时问句里仍带着"华西"，会反复触发、陷入死循环。
        if "region" not in entities:
            cands = [
                c[:-1] if c.endswith("区") else c
                for c in self._REGION_CANDIDATE_RE.findall(question)
            ]
            bad = [c for c in cands if c not in self.REGIONS]
            if bad:
                hints.append(
                    f"「{bad[0]}」不是有效区域。可选：{'、'.join(self.REGIONS)}"
                )

        # ② 有"X率"这样的指标词，但没命中任何已注册指标（"准确率"）。
        #    同样只在 metric 为空时提示（回一句"准时率"就能收敛）。
        if metric is None:
            m = self._RATE_WORD_RE.search(question)
            if m:
                word = self._PREFIX_STOPWORDS_RE.sub("", m.group(0)) or m.group(0)
                names = [x.name for x in self.layer.metrics.values()]
                hints.append(
                    f"没找到叫「{word}」的指标。现在能问的指标有：{'、'.join(names)}"
                )

        # ③ 「X客户的」这种限定写法，但 X客户 没登记在语义层（"某航空客户"）。
        #    只在写成了**限定语**（"...客户的"）时判：这是明确的过滤意图，
        #    静默忽略就等于把范围悄悄放大成全公司 —— 与本文件 ⑥ 步同一族问题。
        #    刻意不泛化（如"最高的客户是哪个"不含"客户的"），以免误伤排名类问句。
        #
        #    ⚠️ 要先剥掉"各/所有/每个/全部"这类前缀：「各客户的合同金额是多少」里
        #    正则抓到的是"各客户"，那是**泛指**（本轮按客户分组），不是具体客户名
        #    （第一版漏了这层剥离，把合法的分组问句判成了未登记客户）。
        if "customer" not in entities:
            known = tuple(self.layer.entity_values.get("customer", ()))
            for cand in self._CUSTOMER_FILTER_RE.findall(question):
                word = cand
                while True:
                    stripped = self._CUSTOMER_PREFIX_RE.sub(
                        "", self._PREFIX_STOPWORDS_RE.sub("", word)
                    )
                    if stripped == word:
                        break
                    word = stripped
                if word in ("客户", "") or word in known:
                    continue
                if known:
                    hints.append(
                        f"「{word}」不是已登记的客户。可选：{'、'.join(known)}"
                    )
                else:
                    hints.append(f"「{word}」不是已登记的客户，无法按客户过滤。")
                break

        return "\n\n".join(hints) or None
