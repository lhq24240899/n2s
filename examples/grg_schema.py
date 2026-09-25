"""计量检测领域知识：LIMS 风格表结构 + 指标体系 + 同义词库 + 业务知识图谱 + 示例库。

这是「语义层」在计量检测场景下的具体实例化。把这份文件替换成你真实的
数仓 schema / 指标口径，即可让通用引擎瞬间"懂"计量检测的业务。

设计取舍：
- 表结构只取演示所需的 9 张核心表（实验室 / 业务线 / 委托单 / 报告 /
  合同 / 客户 / 设备 / 检测记录），真实环境应按数仓星型模型扩展维度表。
- 指标口径用「人话 + SQL 参考」双写，既喂给 LLM 也方便人工审计。
- 示例库带标签（domain/intent/metrics/dimensions/keywords），且同时打
  中文与业务线 code，保证单轮与多轮（上下文注入 code）都能检索命中。
"""
from __future__ import annotations

from nl2sql.glossary import GlossaryEntry
from nl2sql.knowledge import SchemaRegistry, SQLExampleStore
from nl2sql.models import SQLExample, TableSchema
from nl2sql.semantic import (
    KGEdge,
    KGNode,
    KnowledgeGraph,
    Metric,
    MetricLevel,
    SemanticLayer,
    Synonym,
)


# ============================================================
# 1. 表结构（LIMS 风格）
# ============================================================

def build_tables() -> list[TableSchema]:
    return [
        TableSchema(
            name="labs",
            columns={
                "id": "int",
                "name": "varchar",
                "city": "varchar",
                "region": "varchar",  # 华东/华南/华北/...
                "established_year": "int",   # 成立年份
                "equipment_count": "int",    # 设备台数（实验室资源统计）
                "headcount": "int",          # 在职人数
            },
            description="实验室表",
            # name 给真实基地名样例：LLM 不知道枚举值时只能靠字面猜，
            # 实测会出现"问某基地利用率却返回全库平均"（评估集 B02 抓到）。
            sample_values={
                "region": ["华东", "华南", "华北", "华中", "西南", "西北", "东北"],
                "name": [
                    "广电计量检测（广州）有限公司", "广电计量检测（深圳）有限公司",
                    "广电计量检测（北京）有限公司", "广电计量检测（上海）有限公司",
                    "广电计量检测（无锡）有限公司", "广电计量检测（西安）有限公司",
                ],
            },
        ),
        TableSchema(
            name="business_lines",
            columns={
                "id": "int",
                "code": "varchar",  # reliability/emc/ic/calibration/...
                "name": "varchar",  # 可靠性与环境试验/电磁兼容检测/...
            },
            description="业务线表",
            sample_values={
                "code": ["calibration", "reliability", "emc", "ic", "life_science", "data_science", "ehs"]
            },
        ),
        TableSchema(
            name="trust_orders",
            columns={
                "id": "int",
                "lab_id": "int",
                "business_line_id": "int",
                "customer_id": "int",
                "contract_id": "int",
                "created_at": "timestamp",
                "promised_date": "timestamp",  # 合同约定完成时间，用于判定"按期"
                "status": "varchar",
            },
            description="委托单表",
            foreign_keys=[
                ("lab_id", "labs", "id"),
                ("business_line_id", "business_lines", "id"),
                ("customer_id", "customers", "id"),
                ("contract_id", "contracts", "id"),
            ],
            sample_values={"status": ["进行中", "已完成", "已取消"]},
        ),
        TableSchema(
            name="reports",
            columns={
                "id": "int",
                "order_id": "int",
                "lab_id": "int",
                "business_line_id": "int",
                "issued_at": "timestamp",  # 报告出具时间
                "on_time": "int",  # 1=按期出具，0=逾期
                "amount": "decimal",  # 报告对应金额
                "status": "varchar",
            },
            description="报告表",
            foreign_keys=[
                ("order_id", "trust_orders", "id"),
                ("lab_id", "labs", "id"),
                ("business_line_id", "business_lines", "id"),
            ],
            sample_values={"status": ["已出具", "待出具"]},
        ),
        TableSchema(
            name="contracts",
            columns={
                "id": "int",
                "customer_id": "int",
                "signed_at": "timestamp",
                "amount": "decimal",  # 合同金额 = 检测服务收入口径的基数
                "settled_status": "varchar",  # 已开票/未开票
            },
            description="合同表",
            foreign_keys=[("customer_id", "customers", "id")],
            sample_values={"settled_status": ["已开票", "未开票"]},
        ),
        TableSchema(
            name="customers",
            columns={
                "id": "int",
                "name": "varchar",
                "industry": "varchar",  # 战略性新兴产业分类
            },
            description="客户表",
        ),
        TableSchema(
            name="equipment",
            columns={
                "id": "int",
                "lab_id": "int",
                "model": "varchar",  # 设备型号
                "online_status": "int",  # 1=在线 0=离线
                "utilization": "decimal",  # 设备利用率 0~1
            },
            description="设备表",
            foreign_keys=[("lab_id", "labs", "id")],
        ),
        TableSchema(
            name="test_records",
            columns={
                "id": "int",
                "order_id": "int",
                "param": "varchar",  # 检测参数
                "standard": "varchar",  # 标准依据 GB/ISO/IEC 17025
                "pass": "int",  # 1=一次通过 0=不合格
            },
            description="检测记录表",
            foreign_keys=[("order_id", "trust_orders", "id")],
        ),
        TableSchema(
            name="business_segment_revenue",
            columns={
                "id": "int",
                "report_date": "date",          # 报告期（季末：2024-03-31 ... 2026-06-30）
                "business_segment": "varchar",  # 板块名，与 business_lines.name 同义
                "revenue": "decimal",           # 营收（万元）—— **财务口径**
                "revenue_yoy": "decimal",       # 同比（%）
                "gross_margin": "decimal",      # 毛利率（%）
            },
            description="业务板块经营表（财务口径，分季度披露）",
            sample_values={
                "business_segment": [
                    "可靠性与环境试验", "电磁兼容检测", "集成电路测试与分析",
                    "计量服务", "软件测评", "生命科学", "EHS评价服务",
                ]
            },
        ),
    ]


# ============================================================
# 2. 指标体系（三层）
# ============================================================

def build_metrics() -> list[Metric]:
    return [
        Metric(
            id="detect_service_revenue",
            name="检测服务收入",
            level=MetricLevel.GROUP,
            domain="通用",
            definition="SUM(contracts.amount) WHERE reports.已出具 AND contracts.settled_status='已开票'",
            sql_hint=(
                "SELECT SUM(c.amount) AS revenue FROM contracts c "
                "JOIN trust_orders o ON o.contract_id = c.id "
                "JOIN reports r ON r.order_id = o.id "
                "WHERE c.settled_status = '已开票' AND r.status = '已出具'"
            ),
            source_tables=["contracts", "trust_orders", "reports"],
            dimensions=["区域", "实验室", "业务线", "时间"],
            # 口径治理：这里只留**合同/开票**语义的别名。
            # 此前 "营收"/"合同总金额" 挂在收入指标上，导致问「已开票的合同总金额」时
            # 命中本指标（经报告关联）而与"合同表直接求和"冲突——评估集实测抓到过。
            # 现财务口径另立 segment_revenue，"营收"归财务、"合同金额"归合同，各走各路。
            aliases=["收入", "检测收入", "检测服务收入", "开票收入"],
        ),
        Metric(
            id="contract_amount",
            name="合同金额",
            level=MetricLevel.GROUP,
            domain="通用",
            definition=(
                "SUM(contracts.amount)，**合同口径**（可按 settled_status 过滤）。"
                "与『检测服务收入』不同：后者只统计已开票且报告已出具的合同，两者数值不相等，"
                "问『合同总金额』走本口径，不要用收入口径顶替"
            ),
            # sql_hint 刻意**不带** settled_status 过滤：带了会被 LLM 照抄到
            # "每个客户的合同总金额"（不该过滤）上——评估集实测抓到过（C05）。
            # 过滤条件应由问句决定，不写死在口径模板里。
            sql_hint="SELECT SUM(amount) AS contract_amount FROM contracts",
            source_tables=["contracts"],
            dimensions=["区域", "客户", "时间"],
            aliases=["合同金额", "合同总金额", "合同额", "签约金额"],
        ),
        Metric(
            id="segment_revenue",
            name="业务板块营收",
            level=MetricLevel.GROUP,
            domain="经营",
            definition=(
                "**财务口径**的分板块营收（万元），来自 business_segment_revenue.revenue，"
                "按报告期 report_date（季末）披露。与『检测服务收入』（合同/开票口径）不同源，不可混用"
            ),
            sql_hint=(
                "SELECT SUM(revenue) AS revenue FROM business_segment_revenue "
                "WHERE report_date = (SELECT MAX(report_date) FROM business_segment_revenue)"
            ),
            source_tables=["business_segment_revenue"],
            dimensions=["业务板块", "时间"],
            aliases=["营收", "营业额", "营业收入", "板块营收", "经营收入"],
        ),
        Metric(
            id="revenue_yoy",
            name="营收同比",
            level=MetricLevel.GROUP,
            domain="经营",
            definition="business_segment_revenue.revenue_yoy，相对去年同期的增长率（%）",
            sql_hint=(
                "SELECT business_segment, revenue_yoy FROM business_segment_revenue "
                "WHERE report_date = (SELECT MAX(report_date) FROM business_segment_revenue) "
                "ORDER BY revenue_yoy DESC LIMIT 1"
            ),
            source_tables=["business_segment_revenue"],
            dimensions=["业务板块", "时间"],
            aliases=["同比", "同比增长率", "营收增长", "增长率"],
        ),
        Metric(
            id="equipment_stock",
            name="设备保有量",
            level=MetricLevel.OPERATION,
            domain="通用",
            definition=(
                "**资源台账口径**的设备台数：SUM(labs.equipment_count)。"
                "注意与『设备利用率』不同源——后者来自 equipment 表的逐台记录；"
                "问『有多少台设备』应走本口径，不要 COUNT(equipment.id)"
            ),
            sql_hint="SELECT SUM(equipment_count) AS equipment_count FROM labs",
            source_tables=["labs"],
            dimensions=["区域", "实验室"],
            # 注意：不含"多少台设备"——那是 equipment 表逐台台账的 COUNT 口径（评估集 B05/B06），
            # 与本口径（labs 资源台账 1055 台）不是一回事，别名必须切开，否则互相污染。
            aliases=["设备台数", "设备数量", "设备保有量", "设备总数"],
        ),
        Metric(
            id="gross_margin",
            name="毛利率",
            level=MetricLevel.GROUP,
            domain="经营",
            definition="business_segment_revenue.gross_margin，单位 %",
            sql_hint=(
                "SELECT business_segment, gross_margin FROM business_segment_revenue "
                "WHERE report_date = (SELECT MAX(report_date) FROM business_segment_revenue) "
                "ORDER BY gross_margin DESC LIMIT 1"
            ),
            source_tables=["business_segment_revenue"],
            dimensions=["业务板块", "时间"],
            aliases=["毛利率", "毛利", "毛利率水平"],
        ),
        Metric(
            id="on_time_completion_rate",
            name="检测准时率",
            level=MetricLevel.OPERATION,
            domain="通用",
            definition="按期出具报告数 / 总报告数，其中'按期'由 trust_orders.promised_date 与 reports.issued_at 判定",
            sql_hint=(
                "SELECT COUNT(*) FILTER (WHERE r.on_time = 1)::float "
                "/ NULLIF(COUNT(*), 0) AS on_time_rate FROM reports r"
            ),
            source_tables=["reports", "labs", "business_lines"],
            dimensions=["区域", "实验室", "业务线", "时间"],
            aliases=["准时完成率", "准时率", "按期率"],
        ),
        Metric(
            id="report_cycle",
            name="报告出具周期",
            level=MetricLevel.OPERATION,
            domain="通用",
            definition="AVG(reports.issued_at - trust_orders.created_at)，单位天",
            sql_hint=(
                "SELECT AVG(EXTRACT(EPOCH FROM (r.issued_at - o.created_at)) / 86400) "
                "AS cycle_days FROM reports r JOIN trust_orders o ON r.order_id = o.id"
            ),
            source_tables=["reports", "trust_orders"],
            dimensions=["区域", "实验室", "业务线"],
            aliases=["出具周期", "报告周期"],
        ),
        Metric(
            id="equipment_utilization",
            name="设备利用率",
            level=MetricLevel.OPERATION,
            domain="通用",
            definition="AVG(equipment.utilization)，取值 0~1",
            sql_hint="SELECT AVG(e.utilization) AS utilization FROM equipment e",
            source_tables=["equipment", "labs"],
            dimensions=["区域", "实验室"],
            aliases=["利用率", "设备使用率"],
        ),
        Metric(
            id="first_pass_rate",
            name="检测一次通过率",
            level=MetricLevel.QUALITY,
            domain="通用",
            definition="检测一次通过数 / 总检测记录数，基于 test_records.pass",
            sql_hint=(
                "SELECT COUNT(*) FILTER (WHERE t.pass = 1)::float "
                "/ NULLIF(COUNT(*), 0) AS first_pass_rate FROM test_records t"
            ),
            source_tables=["test_records", "trust_orders", "business_lines"],
            dimensions=["区域", "实验室", "业务线"],
            aliases=["一次通过率", "首检合格率"],
        ),
    ]


# ============================================================
# 3. 同义词库（LLM 听不懂的计量检测黑话 -> 标准术语）
# ============================================================

def build_synonyms() -> list[Synonym]:
    return [
        Synonym("EMC", "电磁兼容检测", "business_line", "emc"),
        Synonym("可靠性", "可靠性与环境试验", "business_line", "reliability"),
        Synonym("芯片测试", "集成电路测试与分析", "business_line", "ic"),
        Synonym("集成电路", "集成电路测试与分析", "business_line", "ic"),
        Synonym("软测", "软件测评", "business_line", "data_science"),
        Synonym("计量", "计量服务", "business_line", "calibration"),
        Synonym("生命科学", "生命科学", "business_line", "life_science"),
        Synonym("EHS", "EHS评价服务", "business_line", "ehs"),
        # ---- 业务线**全名**也要登记：用户可能直接说全称（评估集实测，
        #      漏登记时 LLM 拿不到硬约束，会把"按业务线过滤"写成"按业务线分组"）----
        Synonym("可靠性与环境试验", "可靠性与环境试验", "business_line", "reliability"),
        Synonym("电磁兼容检测", "电磁兼容检测", "business_line", "emc"),
        Synonym("集成电路测试与分析", "集成电路测试与分析", "business_line", "ic"),
        Synonym("计量服务", "计量服务", "business_line", "calibration"),
        Synonym("数据科学分析与评价", "数据科学分析与评价", "business_line", "data_science"),
        # 歧义示例：需向用户澄清。
        # canonical 用「可靠性」——用户确认后用它重写原问题，从而复用已有的
        # 「可靠性 -> business_line=reliability」映射，保证消歧后能正确落到指标/业务线。
        Synonym(
            "那个做环境的",
            "可靠性",
            "business_line",
            "reliability",
            ambiguous=True,
            clarification="您说的'那个做环境的'是指『可靠性与环境试验』（环境可靠性实验室）业务线吗？请确认。",
        ),
    ]


# ============================================================
# 4. 业务知识图谱（实体关系，供 Schema Linking 增强）
# ============================================================

def build_knowledge_graph() -> KnowledgeGraph:
    nodes = [
        KGNode("business_line", "可靠性与环境试验", {"code": "reliability"}),
        KGNode("business_line", "电磁兼容检测", {"code": "emc"}),
        KGNode("business_line", "集成电路测试与分析", {"code": "ic"}),
        KGNode("business_line", "计量服务", {"code": "calibration"}),
        KGNode("business_line", "数据科学分析与评价", {"code": "data_science"}),
        KGNode("region", "华东", {}),
        KGNode("region", "华南", {}),
        KGNode("standard", "GB/T 27025", {}),
        KGNode("standard", "ISO/IEC 17025", {}),
    ]
    edges = [
        KGEdge("实验室", "属于", "区域"),
        KGEdge("区域", "属于", "大区"),
        KGEdge("实验室", "拥有", "设备"),
        KGEdge("设备", "关联", "检测项目"),
        KGEdge("检测项目", "依据", "标准"),
        KGEdge("委托单", "包含", "样品"),
        KGEdge("样品", "对应", "检测记录"),
        KGEdge("检测记录", "生成", "报告"),
        KGEdge("客户", "签订", "合同"),
        KGEdge("合同", "关联", "委托单"),

        # ---- 「业务术语 -> 物理表」映射 ----
        # 这一段是 Schema Linking 真正要用的：用户说中文，物理表名是英文，
        # 靠它把「实验室」「设备」这类业务词落到 labs / equipment 上。
        KGEdge("实验室", "对应表", "labs"),
        KGEdge("设备", "对应表", "equipment"),
        KGEdge("委托单", "对应表", "trust_orders"),
        KGEdge("报告", "对应表", "reports"),
        KGEdge("检测记录", "对应表", "test_records"),
        KGEdge("客户", "对应表", "customers"),
        KGEdge("合同", "对应表", "contracts"),
        # 区域落在 labs.region 这一列上，不是独立的表
        KGEdge("区域", "对应表", "labs"),
        KGEdge("华东", "对应表", "labs"),
        KGEdge("华南", "对应表", "labs"),
        KGEdge("华北", "对应表", "labs"),

        # 业务线 -> 数据表（报告表与检测记录表都按业务线关联）
        KGEdge("可靠性与环境试验", "对应表", "reports"),
        KGEdge("电磁兼容检测", "对应表", "reports"),
        KGEdge("集成电路测试与分析", "对应表", "test_records"),
        KGEdge("计量服务", "对应表", "reports"),
        KGEdge("数据科学分析与评价", "对应表", "reports"),
    ]
    return KnowledgeGraph(nodes=nodes, edges=edges)


# ============================================================
# 5. 示例库（带标签，可解释检索）
# ============================================================

def build_examples() -> list[SQLExample]:
    return [
        SQLExample(
            id="ex_lab_equipment_stock",
            question="各实验室的设备台数是多少",
            sql="SELECT name, equipment_count FROM labs ORDER BY name",
            domain=["实验室"],
            intent=["分组"],
            tables=["labs"],
            metrics=["设备保有量"],
            dimensions=["实验室"],
            keywords=["设备台数", "各实验室", "设备数量"],
        ),
        SQLExample(
            id="ex_segment_revenue_latest",
            question="最近一个季度各业务板块的营收是多少",
            sql=(
                "SELECT business_segment, revenue FROM business_segment_revenue "
                "WHERE report_date = (SELECT MAX(report_date) FROM business_segment_revenue) "
                "ORDER BY revenue DESC"
            ),
            domain=["经营"],
            intent=["分组"],
            tables=["business_segment_revenue"],
            metrics=["业务板块营收"],
            dimensions=["业务板块", "时间"],
            keywords=["营收", "各业务板块", "最近一个季度", "板块"],
        ),
        SQLExample(
            id="ex_segment_yoy_top",
            question="哪个业务板块的营收同比增长最快",
            sql=(
                "SELECT business_segment, revenue_yoy FROM business_segment_revenue "
                "WHERE report_date = (SELECT MAX(report_date) FROM business_segment_revenue) "
                "ORDER BY revenue_yoy DESC LIMIT 1"
            ),
            domain=["经营"],
            intent=["排名"],
            tables=["business_segment_revenue"],
            metrics=["营收同比"],
            dimensions=["业务板块", "时间"],
            keywords=["同比", "增长最快", "板块", "营收"],
        ),
        SQLExample(
            id="ex_segment_margin_top",
            question="毛利率最高的业务板块是哪个",
            sql=(
                "SELECT business_segment, gross_margin FROM business_segment_revenue "
                "WHERE report_date = (SELECT MAX(report_date) FROM business_segment_revenue) "
                "ORDER BY gross_margin DESC LIMIT 1"
            ),
            domain=["经营"],
            intent=["排名"],
            tables=["business_segment_revenue"],
            metrics=["毛利率"],
            dimensions=["业务板块", "时间"],
            keywords=["毛利率", "最高", "板块"],
        ),
        SQLExample(
            id="ex_ontime_relia_east",
            question="华东区上个月可靠性试验的准时完成率是多少",
            sql=(
                "SELECT COUNT(*) FILTER (WHERE r.on_time = 1)::float "
                "/ NULLIF(COUNT(*), 0) AS on_time_rate "
                "FROM reports r "
                "JOIN labs l ON r.lab_id = l.id "
                "JOIN business_lines b ON r.business_line_id = b.id "
                "WHERE l.region = '华东' AND b.code = 'reliability' "
                "AND r.issued_at >= CURRENT_DATE - INTERVAL '1 month'"
            ),
            domain=["可靠性", "reliability"],
            intent=["比率"],
            tables=["reports", "labs", "business_lines"],
            metrics=["检测准时率"],
            dimensions=["区域", "业务线", "时间"],
            keywords=["华东", "上个月", "准时完成率", "可靠性试验"],
        ),
        SQLExample(
            id="ex_revenue_relia_east",
            question="华东区上个月可靠性试验的检测服务收入是多少",
            sql=(
                "SELECT SUM(c.amount) AS revenue "
                "FROM contracts c "
                "JOIN trust_orders o ON o.contract_id = c.id "
                "JOIN reports r ON r.order_id = o.id "
                "JOIN labs l ON r.lab_id = l.id "
                "JOIN business_lines b ON r.business_line_id = b.id "
                "WHERE c.settled_status = '已开票' AND r.status = '已出具' "
                "AND l.region = '华东' AND b.code = 'reliability' "
                "AND r.issued_at >= CURRENT_DATE - INTERVAL '1 month'"
            ),
            domain=["可靠性", "reliability"],
            intent=["聚合"],
            tables=["contracts", "trust_orders", "reports", "labs", "business_lines"],
            metrics=["检测服务收入"],
            dimensions=["区域", "业务线", "时间"],
            keywords=["华东", "上个月", "收入", "可靠性试验"],
        ),
        SQLExample(
            id="ex_equip_util",
            question="各实验室设备利用率",
            sql=(
                "SELECT l.name, AVG(e.utilization) AS utilization "
                "FROM equipment e JOIN labs l ON e.lab_id = l.id "
                "GROUP BY l.name ORDER BY utilization DESC"
            ),
            domain=["通用"],
            intent=["聚合"],
            tables=["equipment", "labs"],
            metrics=["设备利用率"],
            dimensions=["实验室"],
            keywords=["设备", "利用率"],
        ),
        SQLExample(
            id="ex_first_pass_ic",
            question="集成电路测试的检测一次通过率",
            sql=(
                "SELECT COUNT(*) FILTER (WHERE t.pass = 1)::float "
                "/ NULLIF(COUNT(*), 0) AS first_pass_rate "
                "FROM test_records t "
                "JOIN trust_orders o ON t.order_id = o.id "
                "JOIN business_lines b ON o.business_line_id = b.id "
                "WHERE b.code = 'ic'"
            ),
            domain=["集成电路", "ic"],
            intent=["比率"],
            tables=["test_records", "trust_orders", "business_lines"],
            metrics=["检测一次通过率"],
            dimensions=["业务线"],
            keywords=["芯片测试", "一次通过率", "ic"],
        ),
    ]


# ============================================================
# 6. 组装
# ============================================================

def build_semantic_layer() -> SemanticLayer:
    base_entries = [
        GlossaryEntry("报告已出具", "reports.status = '已出具'"),
        GlossaryEntry("已开票", "contracts.settled_status = '已开票'"),
        GlossaryEntry("按期", "reports.on_time = 1（按 trust_orders.promised_date 判定）"),
    ]
    return SemanticLayer(
        metrics=build_metrics(),
        synonyms=build_synonyms(),
        graph=build_knowledge_graph(),
        base_entries=base_entries,
    )


def build_registry(dialect: str = "postgres") -> SchemaRegistry:
    return SchemaRegistry(build_tables(), dialect=dialect)


def build_store() -> SQLExampleStore:
    return SQLExampleStore(build_examples())
