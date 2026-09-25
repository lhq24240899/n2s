"""测试用确定性替身（fakes）。

这些替身原本放在生产模块里，用于「无 key / 无数据库也能跑 demo」。现在生产
已强制走真实 LLM / 真实 DB，这些 fake 仅保留在测试里，保证 pytest 可离线、
不花真钱、不碰真库地验证编排逻辑与回退行为。

注意：这是测试基础设施，不是生产代码。请勿在业务路径中导入本模块。
"""
from __future__ import annotations

import re
from typing import Optional

from nl2sql.db import DBRunner
from nl2sql.knowledge import SchemaRegistry
from nl2sql.llm import LLMClient


# ============================================================
# 通用 LLM / DB 替身（原 nl2sql.llm.MockLLM / nl2sql.db.MockDBRunner）
# ============================================================

class MockLLM(LLMClient):
    """演示用确定性 Mock：按关键词返回 SQL。

    对 GMV / TopN 类问题故意返回错误列名 `amount`（真实 schema 只有
    `total_amount`），用来在测试里稳定复现「校验层拦下幻觉 → 重试 → 回退」。
    """

    @staticmethod
    def _question(prompt: str) -> str:
        if "# 任务" in prompt:
            after = prompt.split("# 任务", 1)[1]
            for line in after.splitlines():
                line = line.strip()
                if line:
                    return line
        return prompt

    def generate(self, prompt: str, system: str | None = None) -> str:
        q = self._question(prompt)
        if "GMV" in q or "gmv" in q.lower():
            return "SELECT SUM(amount) AS gmv FROM orders WHERE status='paid'"
        if re.search(r"(前|top|最高|最大|排行)", q, re.IGNORECASE):
            return (
                "SELECT user_id, SUM(amount) AS total FROM orders "
                "WHERE status='paid' GROUP BY user_id ORDER BY total DESC LIMIT 10"
            )
        if ("订单" in q) and re.search(r"(多少|总数|求和|count)", q, re.IGNORECASE):
            return "SELECT COUNT(*) AS cnt FROM orders WHERE status='paid'"
        return "SELECT id, name, region FROM users LIMIT 10"


class MockDBRunner(DBRunner):
    def __init__(self, registry: SchemaRegistry, dialect: str = "postgres"):
        self.registry = registry
        self.dialect = dialect

    def explain(self, sql: str) -> tuple[bool, Optional[str]]:
        import sqlglot
        from sqlglot import exp

        try:
            parsed = sqlglot.parse_one(sql, dialect=self.dialect)
        except Exception as e:  # noqa: BLE001
            return False, f"EXPLAIN 失败: {e}"
        if parsed is None:
            return False, "EXPLAIN 失败: 空语句"

        known = set(self.registry.tables.keys())
        referenced = {t.name for t in parsed.find_all(exp.Table)}
        unknown = referenced - known
        if unknown:
            return False, f"EXPLAIN: 未知表 {sorted(unknown)}"

        table_cols = {n: set(t.columns.keys()) for n, t in self.registry.tables.items()}
        for col in parsed.find_all(exp.Column):
            cname, tbl = col.name, col.table
            if tbl:
                if tbl in table_cols and cname not in table_cols[tbl]:
                    return False, f"EXPLAIN: 列不存在 {tbl}.{cname}"
            else:
                if not any(cname in table_cols[t] for t in referenced):
                    return False, f"EXPLAIN: 列不存在 {cname}"
        return True, None

    def execute(self, sql: str) -> tuple[list[str], list[tuple]]:
        s = sql.upper()
        if "COUNT(" in s:
            return (["cnt"], [(1287,)])
        if "USER_ID" in s and "SUM(" in s:
            return (["user_id", "total"], [(101, 5820.0), (202, 4310.5)])
        if "SUM(" in s:
            return (["gmv"], [(982341.50,)])
        if "USERS" in s:
            return (["id", "name", "region"], [(1, "张三", "华南"), (2, "李四", "华北")])
        return (["result"], [("ok",)])


# ============================================================
# 广电计量业务感知替身（原 examples.grg_engine.GRGMockLLM / GRGSampleDB）
# ============================================================

_BL_CODE_TO_CN = {
    "reliability": "可靠性与环境试验",
    "emc": "电磁兼容检测",
    "ic": "集成电路测试与分析",
    "calibration": "计量服务",
    "data_science": "数据科学分析与评价",
    "life_science": "生命科学",
    "ehs": "EHS评价服务",
}


class GRGMockLLM(LLMClient):
    """业务感知 Mock：按解析出的实体生成「真实列名」SQL，便于复现多轮差异。"""

    REGIONS = ["华东", "华南", "华北", "华中", "西南", "西北", "东北"]

    def __init__(self):
        self.context: dict = {}

    @staticmethod
    def _question(prompt: str) -> str:
        if "# 任务" in prompt:
            after = prompt.split("# 任务", 1)[1]
            for line in after.splitlines():
                line = line.strip()
                if line:
                    return line
        return prompt

    def _region(self, q: str) -> str | None:
        for r in self.REGIONS:
            if r in q:
                return r
        return None

    def _bl(self, q: str) -> str | None:
        for code, cn in _BL_CODE_TO_CN.items():
            if code in q or cn in q:
                return code
        return None

    @staticmethod
    def _metric(q: str) -> str | None:
        # 从问题文本解析指标（生产真实 LLM 不需要，但替身用来生成对应 SQL）
        if re.search(r"准时|按时|on_time|按期率", q):
            return "on_time_completion_rate"
        if re.search(r"收入|营收|revenue", q):
            return "detect_service_revenue"
        if re.search(r"利用率|utilization|设备使用", q):
            return "equipment_utilization"
        if re.search(r"一次通过|首检|first_pass", q):
            return "first_pass_rate"
        if re.search(r"周期|cycle|出具周期", q):
            return "report_cycle"
        return None

    def generate(self, prompt: str, system: str | None = None) -> str:
        q = self._question(prompt)
        # 自行解析并跨轮记忆（与引擎的上下文继承行为一致：本轮能从文本拿就更新，
        # 拿不到就沿用上一轮——从而复现"那华南区呢？"只换区域、指标/业务线沿用）
        region = self._region(q) or self.context.get("region")
        bl = self._bl(q) or self.context.get("business_line")
        metric = self._metric(q) or self.context.get("metric")
        time = "上个月" if "上个月" in q or "上月" in q else self.context.get("time")
        self.context.update(region=region, business_line=bl, metric=metric, time=time)

        time_clause = (
            "AND r.issued_at >= CURRENT_DATE - INTERVAL '1 month'"
            if time == "上个月"
            else ""
        )

        if metric == "on_time_completion_rate":
            return self._ontime(region, bl, time_clause)
        if metric == "detect_service_revenue":
            return self._revenue(region, bl, time_clause)
        if metric == "equipment_utilization":
            return self._equip(region)
        if metric == "first_pass_rate":
            return self._first_pass(bl)
        if metric == "report_cycle":
            return self._cycle(bl)
        return "SELECT COUNT(*) AS cnt FROM reports"

    @staticmethod
    def _ontime(region, bl, time_clause):
        rc = f"AND l.region = '{region}'" if region else ""
        bc = f"AND b.code = '{bl}'" if bl else ""
        return (
            "SELECT COUNT(*) FILTER (WHERE r.on_time = 1)::float "
            "/ NULLIF(COUNT(*), 0) AS on_time_rate "
            "FROM reports r "
            "JOIN labs l ON r.lab_id = l.id "
            "JOIN business_lines b ON r.business_line_id = b.id "
            f"WHERE 1=1 {rc} {bc} {time_clause}"
        )

    @staticmethod
    def _revenue(region, bl, time_clause):
        rc = f"AND l.region = '{region}'" if region else ""
        bc = f"AND b.code = '{bl}'" if bl else ""
        return (
            "SELECT SUM(c.amount) AS revenue "
            "FROM contracts c "
            "JOIN trust_orders o ON o.contract_id = c.id "
            "JOIN reports r ON r.order_id = o.id "
            "JOIN labs l ON r.lab_id = l.id "
            "JOIN business_lines b ON r.business_line_id = b.id "
            f"WHERE c.settled_status = '已开票' AND r.status = '已出具' {rc} {bc} {time_clause}"
        )

    @staticmethod
    def _equip(region):
        rc = f"AND l.region = '{region}'" if region else ""
        return (
            "SELECT AVG(e.utilization) AS utilization "
            "FROM equipment e JOIN labs l ON e.lab_id = l.id "
            f"WHERE 1=1 {rc}"
        )

    @staticmethod
    def _first_pass(bl):
        bc = f"AND b.code = '{bl}'" if bl else ""
        return (
            "SELECT COUNT(*) FILTER (WHERE t.pass = 1)::float "
            "/ NULLIF(COUNT(*), 0) AS first_pass_rate "
            "FROM test_records t "
            "JOIN trust_orders o ON t.order_id = o.id "
            "JOIN business_lines b ON o.business_line_id = b.id "
            f"WHERE 1=1 {bc}"
        )

    @staticmethod
    def _cycle(bl):
        bc = f"AND b.code = '{bl}'" if bl else ""
        return (
            "SELECT AVG(EXTRACT(EPOCH FROM (r.issued_at - o.created_at)) / 86400) "
            "AS cycle_days FROM reports r JOIN trust_orders o ON r.order_id = o.id "
            f"WHERE 1=1 {bc}"
        )


class GRGSampleDB(MockDBRunner):
    """按区域返回差异化样例值，让测试里"华东 vs 华南"可见不同结果。"""

    _RATE = {"华东": 0.923, "华南": 0.887, "华北": 0.901, "华中": 0.912}
    _REV = {"华东": 1284500.0, "华南": 982300.0, "华北": 1102300.0}
    _UTIL = {"华东": 0.81, "华南": 0.76, "华北": 0.79}

    def execute(self, sql: str):
        s = sql.upper()
        m = re.search(r"REGION\s*=\s*'([^']+)'", s)
        region = m.group(1) if m else None

        if "ON_TIME_RATE" in s:
            return (["on_time_rate"], [(self._RATE.get(region, 0.90),)])
        if "REVENUE" in s:
            return (["revenue"], [(self._REV.get(region, 1000000.0),)])
        if "UTILIZATION" in s:
            return (["utilization"], [(self._UTIL.get(region, 0.79),)])
        if "FIRST_PASS_RATE" in s:
            return (["first_pass_rate"], [(0.945,)])
        if "CYCLE_DAYS" in s:
            return (["cycle_days"], [(6.4,)])
        return super().execute(sql)
