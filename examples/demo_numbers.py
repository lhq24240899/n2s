"""演示数字核对脚本：开演前跑一次，把 DEMO.md 里要念的数字全部重算出来。

为什么需要它
------------
`上个月` 在语义层里被翻译成 **滚动 30 天**（`r.issued_at >= CURRENT_DATE - INTERVAL '30 days'`），
所以凡是带时间窗的数字**每天都会变**：

    2026-09-27 实跑：华东 0.8750 / 华南 0.8571 / 华北 0.8333
    2026-09-28 实跑：华东 0.8571 / 华南 0.8571 / 华北 0.8000

而"各业务线"这类**不限时间**的聚合是稳定的。所以：
- 带时间窗的：必须开演前重算，别照文档念；
- 不带时间窗的：可以直接念，但跑一遍也就是几秒。

用法
----
    python examples/demo_numbers.py

输出里还会提示：**哪两个区域的数值不同**——第 2 步演示"多轮继承真的换了区域"
需要两个不一样的数（同一天里两个区域可能碰巧相同，那就换一对）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.cases import t_firstpass, t_ontime  # noqa: E402
from nl2sql.config import get_settings  # noqa: E402
from nl2sql.db import PsycopgRunner  # noqa: E402


def _rate(region: str, days: int = 30) -> float | None:
    """按引擎实际生成的 SQL 口径算（注意：不带 status 过滤，与 t_ontime 可能不同）。"""
    sql = (
        "SELECT COUNT(*) FILTER (WHERE r.on_time = 1)::float / NULLIF(COUNT(*), 0) "
        "FROM reports r JOIN labs l ON r.lab_id = l.id "
        "JOIN business_lines b ON r.business_line_id = b.id "
        f"WHERE l.region = '{region}' AND b.code = 'reliability' "
        f"AND r.issued_at >= CURRENT_DATE - INTERVAL '{days} days'"
    )
    _, rows = RUNNER._query(sql)
    return rows[0][0]


def main() -> int:
    print("=" * 62)
    print("演示数字核对（直接念这一份，不要念文档里的旧数字）")
    print("=" * 62)

    _, rows = RUNNER._query("SELECT CURRENT_DATE")
    print(f"\n数据库当前日期：{rows[0][0]}\n")

    print("[会漂] 区域 × 可靠性 × 近 30 天 —— 每天都变，必须现场重算")
    rates: dict[str, float] = {}
    for region in ("华东", "华南", "华北"):
        v = _rate(region)
        rates[region] = v
        print(f"   {region}  {v:.4f}")

    distinct = {round(v, 4) for v in rates.values()}
    if len(distinct) == len(rates):
        print("   -> 三个数各不相同，第 1/2 步用任意两个都行")
    else:
        pairs = [
            (a, b) for i, a in enumerate(rates) for b in rates if a < b
            and round(rates[a], 4) != round(rates[b], 4)
        ]
        print(f"   -> 有重复值；第 2 步请挑这一对（数值不同才有说服力）：{pairs[:2]}")

    print("\n[稳定] 各业务线 准时率（不限时间）—— 可以直接念")
    _, rows = RUNNER._query(
        "SELECT b.name, COUNT(*) FILTER (WHERE r.on_time = 1)::float "
        "/ NULLIF(COUNT(*), 0) AS rt, COUNT(*) AS n "
        "FROM reports r JOIN business_lines b ON r.business_line_id = b.id "
        "GROUP BY b.name ORDER BY b.name"
    )
    for name, rt, n in rows:
        print(f"   {name:<12} {rt:.4f}  (样本 {n})")

    print("\n[稳定] 其他")
    _, rows = RUNNER._query(t_firstpass("ic"))
    print(f"   集成电路 · 一次通过率      {rows[0][0]:.4f}")
    _, rows = RUNNER._query("SELECT AVG(utilization), COUNT(*) FROM equipment")
    print(f"   全公司设备利用率均值       {rows[0][0]:.4f}  （{rows[0][1]} 台）")

    _, rows = RUNNER._query(
        "SELECT cu.name, SUM(c.amount), COUNT(*) FROM contracts c "
        "JOIN customers cu ON c.customer_id = cu.id "
        "WHERE c.settled_status = '已开票' GROUP BY cu.name ORDER BY 2 DESC"
    )
    print("   已开票合同金额（按客户）")
    for name, amt, n in rows:
        print(f"      {name:<10} {int(amt):>10,}  （{n} 份）")

    _, rows = RUNNER._query(
        "SELECT (SELECT COUNT(*) FROM reports), (SELECT COUNT(*) FROM labs), "
        "(SELECT COUNT(*) FROM equipment), (SELECT COUNT(*) FROM contracts)"
    )
    print(f"   表量级 reports/labs/equipment/contracts = {rows[0]}")

    print("\n[对照] 评估集标准答案 t_ontime（带 status='已出具' 过滤）")
    for region in ("华东", "华南", "华北"):
        _, rows = RUNNER._query(t_ontime(region, "reliability", days=30))
        flag = "一致" if abs(rows[0][0] - rates[region]) < 1e-9 else "★不一致，注意口径"
        print(f"   {region}  {rows[0][0]:.4f}   （与引擎口径{flag}）")

    print("\n提示：若上面任何数字与页面显示的差很多，说明页面连的库不是这一个。")
    print("=" * 62)
    return 0


RUNNER = PsycopgRunner(dsn=get_settings().db.dsn, readonly=True)

if __name__ == "__main__":
    raise SystemExit(main())
