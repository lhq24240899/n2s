"""灌入"有业务灵魂"的数据：业务板块经营数据 + 实验室资源数据。

设计原则（重要，与常见 demo 的关键区别）：
- **维度真实、度量按构造**：板块名/城市/实验室名用真实的广电计量业务口径，
  但营收/毛利率这些数字由**确定性公式**生成，并在这里**打印标准答案**。
  用 faker/random 撒点是demo的常见做法，但那样标准答案每天变，评估集直接作废——
  本项目的两条评估线（SQL 93% / ES 100%）全部建立在"答案可按构造推导"之上，不能破。
- **零新依赖**：只用标准库 + 已在用的 psycopg，不引 faker（随机不可控）。
- **口径显式区分**：本表是**财务口径**的分板块营收；既有 detect_service_revenue 指标是
  **合同口径**的检测服务收入。两者不同源也不同义，注册指标时必须在定义里写清楚。

用法：
    python examples/seed_business_data.py               # 建表 + 灌数 + 打印标准答案
    python examples/seed_business_data.py --only-print  # 只看标准答案（不连库）
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---- 业务板块（真实口径，对齐 business_lines.name）+ 构造参数 ----
# (板块名, code, 季度基准营收万元, 基准毛利率%)
SEGMENTS: list[tuple[str, str, float, float]] = [
    ("可靠性与环境试验", "reliability", 12000.0, 45.0),
    ("电磁兼容检测", "emc", 6500.0, 52.0),
    ("集成电路测试与分析", "ic", 5200.0, 58.0),
    ("计量服务", "calibration", 7600.0, 55.0),
    ("软件测评", "data_science", 3800.0, 62.0),
    ("生命科学", "life_science", 2900.0, 48.0),
    ("EHS评价服务", "ehs", 2100.0, 40.0),
]

# 季度序列：2024Q1 ~ 2026Q2（第 10 期为"最近一个季度"）
QUARTER_ENDS: list[date] = [
    date(2024, 3, 31), date(2024, 6, 30), date(2024, 9, 30), date(2024, 12, 31),
    date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31),
    date(2026, 3, 31), date(2026, 6, 30),
]

# 季节性：Q1 受春节影响偏低，Q3 偏高（确定性，不用随机）
SEASONAL = [0.88, 1.02, 1.06, 1.04]
# 各板块逐年增速**必须差异化**：都用一个增速会让"哪个板块增长最快"并列无解，
# 演示时这类问句必被问到，且评估无法判断模型是否真的排了序。
# 取值贴合业务常识：集成电路/软件测评（新兴）高增，计量/EHS（成熟）平稳。
YEAR_GROWTH = {
    "reliability": 0.025,
    "emc": 0.018,
    "ic": 0.055,
    "calibration": 0.012,
    "data_science": 0.048,
    "life_science": 0.035,
    "ehs": 0.020,
}
# 最早 4 期无去年同期，用构造的披露同比（%）
SEED_YOY = {"reliability": 12.5, "emc": 8.0, "ic": 18.2, "calibration": 6.4,
            "data_science": 22.1, "life_science": 15.7, "ehs": 4.3}

DDL = [
    """CREATE TABLE IF NOT EXISTS business_segment_revenue (
        id int,
        report_date date,
        business_segment varchar,
        revenue numeric,
        revenue_yoy numeric,
        gross_margin numeric
    )""",
    # 扩展既有 labs，而不是新建 lab_info：
    # 同名语义的两张表会让 Schema Linking 摇摆（都能答"实验室有多少人"），是反模式
    "ALTER TABLE labs ADD COLUMN IF NOT EXISTS established_year int",
    "ALTER TABLE labs ADD COLUMN IF NOT EXISTS equipment_count int",
    "ALTER TABLE labs ADD COLUMN IF NOT EXISTS headcount int",
]

# 实验室资源：按 id 确定性推导（不用随机，保证可复算）
def _lab_resource(lab_id: int) -> tuple[int, int, int]:
    return (
        2010 + (lab_id * 7) % 15,          # established_year
        100 + (lab_id * 37) % 700,         # equipment_count
        50 + (lab_id * 53) % 450,          # headcount
    )


def revenue_of(q: int, base: float, growth: float) -> float:
    return round(base * SEASONAL[q % 4] * (1 + growth * (q // 4)), 2)


def generate_rows() -> list[tuple]:
    """生成 (id, report_date, segment, revenue, yoy, margin)。"""
    rows: list[tuple] = []
    rid = 0
    for q, d in enumerate(QUARTER_ENDS):
        for name, code, base, margin_base in SEGMENTS:
            rid += 1
            rev = revenue_of(q, base, YEAR_GROWTH[code])
            if q >= 4:
                prev = revenue_of(q - 4, base, YEAR_GROWTH[code])
                yoy = round((rev / prev - 1) * 100, 2)
            else:
                yoy = SEED_YOY[code]
            margin = round(margin_base + (q % 4 - 1.5) * 0.4, 2)
            rows.append((rid, d, name, rev, yoy, margin))
    return rows


def expected(rows: list[tuple]) -> str:
    """打印按构造的标准答案（评估集直接据此写标准 SQL）。"""
    latest = QUARTER_ENDS[-1]
    cur = [r for r in rows if r[1] == latest]
    by_rev = max(cur, key=lambda r: r[3])
    by_margin = max(cur, key=lambda r: r[5])
    by_yoy = max(cur, key=lambda r: r[4])
    y2025 = [r for r in rows if r[1].year == 2025]
    total_2025 = round(sum(r[3] for r in y2025), 2)
    lines = [
        "# 标准答案（按构造推导，可复算）",
        f"# 最近季度 {latest.isoformat()} 各板块营收（万元）:",
    ]
    lines += [f"#   {r[2]}: {r[3]}（同比 {r[4]}%, 毛利率 {r[5]}%）" for r in cur]
    lines += [
        f"# 营收最高板块: {by_rev[2]} = {by_rev[3]}",
        f"# 毛利率最高板块: {by_margin[2]} = {by_margin[5]}%",
        f"# 同比最高板块: {by_yoy[2]} = {by_yoy[4]}%",
        f"# 2025 全年营收合计: {total_2025}",
        "# 实验室资源（按 id 推导）: " + ", ".join(
            f"lab{lid} 设备{_lab_resource(lid)[1]}台/人员{_lab_resource(lid)[2]}人"
            for lid in range(1, 6)),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="灌业务经营 + 实验室资源数据")
    parser.add_argument("--only-print", action="store_true")
    args = parser.parse_args(argv)

    rows = generate_rows()
    print(f"生成 {len(rows)} 行板块经营数据（{len(QUARTER_ENDS)} 期 × {len(SEGMENTS)} 板块）")
    print(expected(rows))
    if args.only_print:
        return 0

    import psycopg

    from nl2sql.config import get_settings

    conn = psycopg.connect(get_settings().db.dsn, connect_timeout=20, autocommit=True)
    cur = conn.cursor()
    for ddl in DDL:
        cur.execute(ddl)
    cur.execute("DELETE FROM business_segment_revenue")   # 幂等：重跑覆盖
    cur.executemany(
        "INSERT INTO business_segment_revenue "
        "(id, report_date, business_segment, revenue, revenue_yoy, gross_margin) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        rows,
    )
    ids = [r[0] for r in cur.execute("SELECT id FROM labs ORDER BY id").fetchall()]
    for lid in ids:
        y, eq, hc = _lab_resource(lid)
        cur.execute(
            "UPDATE labs SET established_year=%s, equipment_count=%s, headcount=%s WHERE id=%s",
            (y, eq, hc, lid),
        )
    print(f"\n已写入 {len(rows)} 行到 business_segment_revenue；已更新 {len(ids)} 个实验室资源字段")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
