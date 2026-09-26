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
    cur.executemany(
        "INSERT INTO contracts (id, customer_id, signed_at, amount, settled_status) "
        "VALUES (%s,%s,%s,%s,%s)",
        [
            (1, 1, ago(40), 500000, "已开票"),
            (2, 2, ago(38), 300000, "已开票"),
            (3, 1, ago(35), 450000, "未开票"),
        ],
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
    for bl_id, lab_ids, cnt, late_idx in PLANS:
        for i in range(cnt):
            lab = lab_ids[i % len(lab_ids)]
            orders.append((oid, lab, bl_id, 1 + (i % 2), (i % 3) + 1))
            # issued_at 落在 10~24 天前：既在「最近 30 天（上个月）」窗口内，
            # 又在「最近 7 天」窗口外 —— 后者用于验证时间窗口边界（E06 期望 0）。
            reports.append(
                (rid, oid, lab, bl_id, ago(10 + i), 0 if i in late_idx else 1, 42000, "已出具")
            )
            oid += 1
            rid += 1

    # 集成电路单独补一条委托单，供 test_records 关联（保持检测记录的外键关系）
    ic_order_id = oid
    orders.append((oid, 4, 4, 1, 1))
    oid += 1

    cur.executemany(
        "INSERT INTO trust_orders "
        "(id, lab_id, business_line_id, customer_id, contract_id, created_at, promised_date, status) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        [(o[0], o[1], o[2], o[3], o[4], ago(30), ago(20), "已完成") for o in orders],
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
