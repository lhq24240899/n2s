"""在真实数据库（.env 的 DB__DSN）中创建示例表并灌入可复现的种子数据。

用途：让 `demo.py`（orders / users）与 `grg_demo.py`（计量检测 9 张表）能端到端
真实跑通。这些表是「演示用」数据，与生产无关，**随时可整体 DROP**（见脚本底部
的 TEARDOWN 说明，或直接 DROP TABLE 列出的表名）。

运行：
    python examples/setup_dev_db.py          # 建表 + 灌数据（幂等：先 DROP 同名表）
    python examples/setup_dev_db.py --keep   # 保留已有数据，仅当表不存在时创建

安全说明：本脚本只操作以下固定表名，不会触碰库中其他表。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import psycopg

from nl2sql.config import get_settings

# 本脚本创建/管理的全部表（仅这些表会被 DROP / 重建）
GRG_TABLES = [
    "test_records", "reports", "equipment", "trust_orders",
    "contracts", "customers", "business_lines", "labs",
]
GENERIC_TABLES = ["orders", "users"]


def _ddl() -> list[str]:
    return [
        # ---------- 通用示例表（demo.py 用） ----------
        "CREATE TABLE users (id int, name varchar, region varchar)",
        "CREATE TABLE orders ("
        "  id int, user_id int, total_amount numeric, status varchar, created_at timestamp)",

        # ---------- 计量检测 9 张核心表 ----------
        "CREATE TABLE labs ("
        "  id int, name varchar, city varchar, region varchar)",
        "CREATE TABLE business_lines (id int, code varchar, name varchar)",
        "CREATE TABLE customers (id int, name varchar, industry varchar)",
        "CREATE TABLE contracts ("
        "  id int, customer_id int, signed_at timestamp, amount numeric, settled_status varchar)",
        "CREATE TABLE trust_orders ("
        "  id int, lab_id int, business_line_id int, customer_id int, contract_id int,"
        "  created_at timestamp, promised_date timestamp, status varchar)",
        "CREATE TABLE reports ("
        "  id int, order_id int, lab_id int, business_line_id int,"
        "  issued_at timestamp, on_time int, amount numeric, status varchar)",
        "CREATE TABLE equipment ("
        "  id int, lab_id int, model varchar, online_status int, utilization numeric)",
        "CREATE TABLE test_records ("
        "  id int, order_id int, param varchar, standard varchar, pass int)",
    ]


def _seed(cur: psycopg.Cursor) -> None:
    now = datetime.now()
    ago = lambda n: now - timedelta(days=n)  # noqa: E731

    cur.executemany(
        "INSERT INTO labs (id, name, city, region) VALUES (%s,%s,%s,%s)",
        [
            # 区域必须与真实基地一致（lab 1 广州 = 华南！）。
            # 早期版本把 lab 1 写成华东，导致后续「华东可靠性报告」有半数挂到华南、
            # 华东因此样本变少且一条逾期都没有 —— 算出「华东准时率 100%」的假数据。
            (1, "广州计量实验室", "广州", "华南"),
            (2, "深圳可靠性实验室", "深圳", "华南"),
            (3, "北京电磁兼容实验室", "北京", "华北"),
            (4, "上海集成电路实验室", "上海", "华东"),
            (5, "无锡可靠性实验室", "无锡", "华东"),
        ],
    )
    cur.executemany(
        "INSERT INTO business_lines (id, code, name) VALUES (%s,%s,%s)",
        [
            (1, "calibration", "计量服务"),
            (2, "reliability", "可靠性与环境试验"),
            (3, "emc", "电磁兼容检测"),
            (4, "ic", "集成电路测试与分析"),
            (5, "life_science", "生命科学"),
            (6, "data_science", "数据科学分析与评价"),
            (7, "ehs", "EHS评价服务"),
        ],
    )
    cur.executemany(
        "INSERT INTO customers (id, name, industry) VALUES (%s,%s,%s)",
        [(1, "某汽车客户", "汽车"), (2, "某通信客户", "通信")],
    )
    # 合同：**每个（业务线 × 区域）块各挂 2 份自己的合同**（早期/晚期各一份），
    # 这是"收入"类问题能有真实差异的前提，两轮踩坑记录：
    #   ① 最初每块都用同一个 (i%3)+1 循环 → 华东/华南收入算出同一个数；
    #   ② 改成"按块偏移"仍然不够 —— 6 份合同被所有块共用，一旦把重复累加修正为
    #      **按合同去重**（一份合同只能计一次收入），各区域收入又全变成同一个数。
    # 现在：合同池 14 份 = 7 个块 × (早期/晚期)，块之间不共用 -> 区域、业务线各不相同；
    #       同一块内「早期合同」只被最近 30 天内的报告关联、「晚期合同」只在 30 天外，
    #       于是时间窗口也能真正改变收入口径（近 30 天 vs 不限时间结果不同）。
    CONTRACTS = [
        # (客户, 签于 N 天前, 金额, 开票状态)  —— 1 份未开票/块附近，供"已开票"过滤体现差异
        (1, 40, 500000, "已开票"), (1, 36, 240000, "已开票"),      # 块0 华东·可靠性
        (2, 40, 300000, "已开票"), (2, 36, 410000, "未开票"),      # 块1 华南·可靠性
        (1, 40, 620000, "已开票"), (2, 36, 180000, "已开票"),      # 块2 华北·可靠性
        (2, 40, 280000, "已开票"), (1, 36, 330000, "已开票"),      # 块3 华南·计量服务
        (1, 40, 350000, "已开票"), (2, 36, 760000, "已开票"),      # 块4 华北·EMC
        (2, 40, 450000, "未开票"), (1, 36, 290000, "已开票"),      # 块5 华东·集成电路
        (1, 40, 210000, "已开票"), (2, 36, 530000, "已开票"),      # 块6 华南·数据科学
    ]
    cur.executemany(
        "INSERT INTO contracts (id, customer_id, signed_at, amount, settled_status) "
        "VALUES (%s,%s,%s,%s,%s)",
        [(i + 1, c[0], ago(c[1]), c[2], c[3]) for i, c in enumerate(CONTRACTS)],
    )

    # 委托单 + 报告：按「业务线 × 区域」确定性构造，供准时率/收入/分组/排名类问题使用。
    #
    # 两个刻意为之、别改回去的设计：
    #  1) **实验室必须按 labs 表的真实区域挂**：
    #     lab 1=广州(华南)、2=深圳(华南)、3=北京(华北)、4=上海(华东)、5=无锡(华东)。
    #     早期版本把「华东」写成 lab 1/5，而 lab 1 实为广州(华南) →
    #     华东的报告少了一半、且逾期样本全落到了华南，于是算出「华东准时率 100%」这种假数据。
    #  2) **样本量要够、逾期要分散**：单区域只有几条报告时，1 条逾期就能让比率跳动 15+ 个百分点；
    #     并且要保证**没有任何「业务线 × 区域」组合是 100%**（真实检测机构不可能零逾期）。
    # 逾期位置用固定下标指定（确定性，不用随机），保证评估集标准答案可复现、可解释。
    #  预期准时率：华东可靠性 13/15、华南可靠性 13/14、华北可靠性 9/11、
    #             计量服务 8/10、EMC 9/12、集成电路 8/9、数据科学 5/6；整体 65/77 ≈ 84%。
    PLANS = [
        # (业务线 id, 实验室 id 列表, 报告数, 逾期下标)
        (2, [4, 5], 15, {0, 9}),     # 可靠性与环境试验 —— 华东（上海/无锡）
        (2, [1, 2], 14, {1}),        # 可靠性与环境试验 —— 华南（广州/深圳）
        (2, [3],    11, {0, 6}),     # 可靠性与环境试验 —— 华北（北京）
        (1, [1],    10, {0, 5}),     # 计量服务     —— 华南（广州）
        (3, [3],    12, {0, 4, 9}),  # 电磁兼容检测 —— 华北（北京）
        (4, [4],     9, {3}),        # 集成电路     —— 华东（上海）
        (6, [1],     6, {1}),        # 数据科学     —— 华南（广州）
    ]

    orders: list[tuple] = []
    reports: list[tuple] = []
    oid = rid = 1
    # 报告出具时间跨度：最早 10 天前、最晚 50 天前。
    # 为什么不再全部压在 10~24 天：
    #   ① 全部落在 30 天内 → 「上个月」与「不限时间」结果完全相同，时间维度在多轮演示里形同虚设；
    #   ② 跨到 50 天后期，两个窗口的差异才真实可见；同时保持「最近 7 天」为空（E06 期望 0 的边界用例）。
    SPAN_START, SPAN_END = 10, 50
    IN_WINDOW_DAYS = 30       # 「上个月/近 30 天」窗口
    for block, (bl_id, lab_ids, cnt, late_idx) in enumerate(PLANS):
        for i in range(cnt):
            lab = lab_ids[i % len(lab_ids)]
            issued_offset = SPAN_START + round(i * (SPAN_END - SPAN_START) / max(cnt - 1, 1))
            # 合同按块分配，且**用时间决定用块内哪一份**：
            #   窗口内(<=30天)的报告 → 块内第 1 份("早期合同")；窗口外 → 第 2 份。
            # 这样「近 30 天收入」只含早期合同、「不限时间收入」含两份 —— 时间维度对收入也真实生效。
            slot = 0 if issued_offset <= IN_WINDOW_DAYS else 1
            cid = block * 2 + slot + 1
            late = i in late_idx
            # ⚠️ promised_date 必须与 on_time **自洽**：指标定义写的是「按期 = 出具日 <= 承诺日」，
            #    早期版本把 promised_date 一律写成 20 天前、on_time 却按 i 硬编码，
            #    结果 53 行 on_time=1 的数据按定义算是逾期 —— 一旦 LLM 依定义用日期比较生成 SQL，
            #    算出的准时率与用 on_time 列算的完全不同（自查发现，见 README 的口径审计）。
            promised_offset = issued_offset - 4 if not late else issued_offset + 3
            orders.append((oid, lab, bl_id, 1 + (i % 2), cid, 95, promised_offset))
            reports.append(
                (rid, oid, lab, bl_id, ago(issued_offset), 0 if late else 1, 42000, "已出具")
            )
            oid += 1
            rid += 1

    # 集成电路单独补一条委托单，供 test_records 关联（保持检测记录的外键关系）
    ic_order_id = oid
    orders.append((oid, 4, 4, 1, 12, 95, 20))
    oid += 1

    cur.executemany(
        "INSERT INTO trust_orders "
        "(id, lab_id, business_line_id, customer_id, contract_id, created_at, promised_date, status) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        # created_at 统一 95 天前：必须早于所有 promised_date（承诺日期不能早于委托创建）
        [(o[0], o[1], o[2], o[3], o[4], ago(o[5]), ago(o[6]), "已完成") for o in orders],
    )
    cur.executemany(
        "INSERT INTO reports "
        "(id, order_id, lab_id, business_line_id, issued_at, on_time, amount, status) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        reports,
    )

    # 检测记录：集成电路（ic_order_id）一批，pass 有 0 有 1
    tr = [(tid, ic_order_id, "耐压", "GB/T 27025", 0 if tid % 11 == 0 else 1)
          for tid in range(1, 21)]
    cur.executemany(
        "INSERT INTO test_records (id, order_id, param, standard, pass) VALUES (%s,%s,%s,%s,%s)",
        tr,
    )

    # 设备：各实验室利用率（华东 ~0.8，华南 ~0.76）
    cur.executemany(
        "INSERT INTO equipment (id, lab_id, model, online_status, utilization) VALUES (%s,%s,%s,%s,%s)",
        [
            (1, 1, "示波器A", 1, 0.82), (2, 1, "电源B", 1, 0.80),
            (3, 5, "温箱C", 1, 0.83), (4, 2, "振动台D", 1, 0.75),
            (5, 2, "频谱仪E", 0, 0.77), (6, 3, "EMC腔F", 1, 0.79),
            (7, 4, "探针G", 1, 0.88),
        ],
    )

    # 通用表（demo.py）
    cur.executemany(
        "INSERT INTO users (id, name, region) VALUES (%s,%s,%s)",
        [(1, "张三", "华南"), (2, "李四", "华北"), (3, "王五", "华东")],
    )
    cur.executemany(
        "INSERT INTO orders (id, user_id, total_amount, status, created_at) VALUES (%s,%s,%s,%s,%s)",
        [
            (1, 1, 199.0, "paid", ago(1)),
            (2, 2, 299.0, "paid", ago(2)),
            (3, 3, 399.0, "refunded", ago(1)),
        ],
    )


def main() -> None:
    settings = get_settings()
    if not settings.db.dsn:
        raise SystemExit("未配置 DB__DSN，无法建表。请在 .env 填入数据库连接串。")

    all_tables = GRG_TABLES + GENERIC_TABLES
    keep = "--keep" in sys.argv

    with psycopg.connect(settings.db.dsn, connect_timeout=15, sslmode="require") as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            if not keep:
                for t in all_tables:
                    cur.execute(f"DROP TABLE IF EXISTS {t}")
                print("已丢弃旧示例表（共 %d 张）" % len(all_tables))
            for ddl in _ddl():
                cur.execute(ddl)
            print("已创建示例表（%d 张）" % len(_ddl()))
            _seed(cur)
            print("已灌入种子数据。")
    print("\n完成。可运行：")
    print("  python examples/grg_demo.py   # 计量检测 4 场景端到端")
    print("  python examples/demo.py       # 通用 3 场景端到端")
    print("\nTEARDOWN（如需清空）：")
    print("  DROP TABLE " + ", ".join(all_tables) + ";")


if __name__ == "__main__":
    main()
