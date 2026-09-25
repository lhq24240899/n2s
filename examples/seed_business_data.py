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

# ---- 业务板块（广电计量 002967 的真实业务结构）----
# 板块名对齐 business_lines.name；季度基准营收单位万元。
# 量级按该集团公开年报口径模拟：年营收约 32 亿（= 季度合计 8 亿 ≈ 80000 万元），
# 结构与真实盘面一致——计量校准 + 可靠性与环境试验是两大主力，合计约占六成。
# 毛利率参考检测行业常识：计量/可靠性（重资产、设备折旧高）偏低，软件测评/集成电路（人力密集）偏高。
# (板块名, code, 季度基准营收万元, 基准毛利率%)
SEGMENTS: list[tuple[str, str, float, float]] = [
    ("计量服务", "calibration", 24000.0, 48.0),
    ("可靠性与环境试验", "reliability", 20000.0, 44.0),
    ("电磁兼容检测", "emc", 11000.0, 53.0),
    ("集成电路测试与分析", "ic", 9500.0, 57.0),
    ("生命科学", "life_science", 6500.0, 50.0),
    ("软件测评", "data_science", 5000.0, 62.0),
    ("EHS评价服务", "ehs", 4000.0, 41.0),
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

# ---- 实验室/基地网络：按该集团真实的全国布局（总部广州 + 各区域基地）----
# (名称, 城市, 区域, 成立年份, 设备台数, 在职人数)
# 规模按真实梯度：总部最大，华东/华南基地次之，新设基地较小；
# 合计约 4415 台设备 / 3315 人，与该集团公开的"数千台套、数千员工"量级一致。
LABS: list[tuple[str, str, str, int, int, int]] = [
    ("广电计量检测（广州）有限公司", "广州", "华南", 2002, 860, 620),   # 总部
    ("广电计量检测（深圳）有限公司", "深圳", "华南", 2011, 420, 310),
    ("广电计量检测（北京）有限公司", "北京", "华北", 2010, 380, 290),
    ("广电计量检测（上海）有限公司", "上海", "华东", 2012, 350, 260),
    ("广电计量检测（无锡）有限公司", "无锡", "华东", 2013, 300, 220),
    ("广电计量检测（西安）有限公司", "西安", "西北", 2015, 210, 160),
    ("广电计量检测（武汉）有限公司", "武汉", "华中", 2016, 190, 150),
    ("广电计量检测（成都）有限公司", "成都", "西南", 2015, 200, 155),
    ("广电计量检测（天津）有限公司", "天津", "华北", 2017, 160, 120),
    ("广电计量检测（青岛）有限公司", "青岛", "华北", 2018, 140, 105),
    ("广电计量检测（南京）有限公司", "南京", "华东", 2016, 175, 135),
    ("广电计量检测（苏州）有限公司", "苏州", "华东", 2018, 150, 110),
    ("广电计量检测（杭州）有限公司", "杭州", "华东", 2019, 130, 100),
    ("广电计量检测（长沙）有限公司", "长沙", "华中", 2017, 145, 115),
    ("广电计量检测（沈阳）有限公司", "沈阳", "华北", 2019, 120, 90),
    ("广电计量检测（重庆）有限公司", "重庆", "西南", 2020, 110, 85),
    ("广电计量检测（郑州）有限公司", "郑州", "华中", 2020, 105, 80),
    ("广电计量检测（合肥）有限公司", "合肥", "华东", 2021, 95, 75),
    ("广电计量检测（厦门）有限公司", "厦门", "华南", 2021, 90, 70),
    ("广电计量检测（东莞）有限公司", "东莞", "华南", 2022, 85, 65),
]


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
    ]
    total_eq = sum(l[4] for l in LABS)
    total_hc = sum(l[5] for l in LABS)
    by_region: dict[str, int] = {}
    for l in LABS:
        by_region[l[2]] = by_region.get(l[2], 0) + l[4]
    lines += [
        f"# 实验室网络: {len(LABS)} 个基地，设备合计 {total_eq} 台，人员合计 {total_hc} 人",
        "# 各区域设备数: " + ", ".join(f"{k}={v}" for k, v in sorted(by_region.items())),
        f"# 设备最多的基地: {max(LABS, key=lambda l: l[4])[0]} = {max(LABS, key=lambda l: l[4])[4]} 台",
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
    # 实验室网络：前 5 个沿用现有 id（保住 reports/equipment 的外键关联），改写为真实基地；
    # 其余基地按 id 递增新增。重跑幂等（先清空新增部分）。
    cur.execute("SELECT COALESCE(MAX(id), 0) FROM labs")
    max_id = cur.fetchone()[0]
    if max_id > len(LABS):
        cur.execute("DELETE FROM labs WHERE id > %s", (len(LABS),))
    for idx, (name, city, region, year, eq, hc) in enumerate(LABS, start=1):
        cur.execute("SELECT 1 FROM labs WHERE id = %s", (idx,))
        if cur.fetchone():
            cur.execute(
                "UPDATE labs SET name=%s, city=%s, region=%s, "
                "established_year=%s, equipment_count=%s, headcount=%s WHERE id=%s",
                (name, city, region, year, eq, hc, idx),
            )
        else:
            cur.execute(
                "INSERT INTO labs (id, name, city, region, "
                "established_year, equipment_count, headcount) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (idx, name, city, region, year, eq, hc),
            )
    print(f"\n已写入 {len(rows)} 行到 business_segment_revenue；"
          f"实验室网络 {len(LABS)} 个基地已就绪")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
