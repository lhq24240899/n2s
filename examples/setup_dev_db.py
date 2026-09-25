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
            (1, "广州计量实验室", "广州", "华东"),
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
    cur.executemany(
        "INSERT INTO contracts (id, customer_id, signed_at, amount, settled_status) "
        "VALUES (%s,%s,%s,%s,%s)",
        [
            (1, 1, ago(40), 500000, "已开票"),
            (2, 2, ago(38), 300000, "已开票"),
            (3, 1, ago(35), 450000, "未开票"),
        ],
    )

    # 委托单：先造足量的「可靠性」委托单，分属 华东/华南/华北 实验室，
    # 报告才能通过 r.order_id = o.id 正确关联（避免孤儿外键导致 JOIN 掉数据）。
    orders = []
    oid = 1
    # 华东可靠性委托单（lab 1 / 5）
    for i in range(15):
        lab = 1 if i % 2 == 0 else 5
        orders.append((oid, lab, 2, 1 if i % 2 == 0 else 2, (i % 3) + 1))
        oid += 1
    # 华南可靠性委托单（lab 2）
    for i in range(9):
        orders.append((oid, 2, 2, 1 if i % 2 == 0 else 2, (i % 3) + 1))
        oid += 1
    # 华北可靠性委托单（lab 3）
    for i in range(10):
        orders.append((oid, 3, 2, 1 if i % 2 == 0 else 2, (i % 3) + 1))
        oid += 1
    # 集成电路委托单（lab 4，供 test_records 关联）
    ic_order_id = oid
    orders.append((oid, 4, 4, 1, 1))
    oid += 1

    # 其他业务线的委托单：让「各业务线的检测准时率」这类分组问题能返回多行，
    # 而不是只剩"可靠性"一条线。各线数量与逾期数不同，便于演示"每条线不一样"。
    extra_start = len(orders)
    other_lines = [
        (1, 1, 8, 1),    # 计量服务     -> 广州计量实验室,      8 单 / 1 条逾期
        (3, 3, 10, 2),   # 电磁兼容检测 -> 北京电磁兼容实验室, 10 单 / 2 条逾期
        (4, 4, 6, 1),    # 集成电路     -> 上海集成电路实验室,  6 单 / 1 条逾期
        (6, 1, 5, 1),    # 数据科学     -> 广州计量实验室,      5 单 / 1 条逾期
    ]
    for bl_id, lab_id, cnt, _late in other_lines:
        for _ in range(cnt):
            orders.append((oid, lab_id, bl_id, 1, 1))
            oid += 1

    cur.executemany(
        "INSERT INTO trust_orders "
        "(id, lab_id, business_line_id, customer_id, contract_id, created_at, promised_date, status) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        [(o[0], o[1], o[2], o[3], o[4], ago(30), ago(20), "已完成") for o in orders],
    )

    # 报告：每一条都挂在对应区域的可靠性委托单上；每个区域首批各留 1 条逾期(on_time=0)，
    # 使「华东 vs 华南 vs 华北」准时率不同（演示多轮上下文与区域替换）。
    # 华东 12 条(11 准时) / 华南 9 条(8 准时) / 华北 10 条(9 准时)
    east = orders[0:15]
    south = orders[15:24]
    north = orders[24:34]
    reports = []
    rid = 1

    def add_report(order, on_time, day_offset):
        nonlocal rid
        reports.append((rid, order[0], order[1], order[2], ago(day_offset), on_time, 42000, "已出具"))
        rid += 1

    for i, o in enumerate(east[:12]):
        add_report(o, 0 if i == 0 else 1, 10 + i)
    for i, o in enumerate(south[:9]):
        add_report(o, 0 if i == 0 else 1, 10 + i)
    for i, o in enumerate(north[:10]):
        add_report(o, 0 if i == 0 else 1, 10 + i)

    # 其他业务线的报告：每条线逾期数不同 -> 「各业务线准时率」结果彼此有差异
    pos = extra_start
    for _bl_id, _lab_id, cnt, late in other_lines:
        for j in range(cnt):
            add_report(orders[pos], 0 if j < late else 1, 10 + j)
            pos += 1

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
