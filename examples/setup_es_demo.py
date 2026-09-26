"""给 ES 灌设备日志演示数据：索引映射 + **按构造**生成可预知答案的数据。

为什么"按构造"很重要：
  评估需要标准答案。这里的各类计数（如"华东区最近7天 ERROR 告警 = 42 条"）
  是**生成时写死的常量**，跑完打印出来 —— 之后无论用 ES 查还是写进评估集，答案都精确已知。

用法：
    python examples/setup_es_demo.py                # 建索引 + 灌数据 + 打印标准答案
    python examples/setup_es_demo.py --only-print   # 只打印标准答案（不连集群）

数据形状（约 1,500 条，够演示、灌得快）：
  device_events: lab_name / region / business_line / level / device_id / message / ts
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nl2sql.config import get_settings

# ---- 按构造的精确数量（标准答案的来源，勿随意改） ----
LABS = [  # (实验室, 区域, 业务线code)
    ("上海集成电路实验室", "华东", "ic"),
    ("深圳可靠性实验室", "华南", "reliability"),
    ("北京电磁兼容实验室", "华北", "emc"),
]
LAST7 = {  # region -> {level: count}，最近 7 天
    "华东": {"ERROR": 42, "WARN": 60, "INFO": 200},
    "华南": {"ERROR": 30, "WARN": 45, "INFO": 150},
    "华北": {"ERROR": 25, "WARN": 40, "INFO": 120},
}
D8_30 = {  # 第 8~30 天
    "华东": {"ERROR": 18, "WARN": 40, "INFO": 150},
    "华南": {"ERROR": 12, "WARN": 30, "INFO": 120},
    "华北": {"ERROR": 10, "WARN": 30, "INFO": 100},
}
MSG = {
    "ERROR": ["温度超限告警", "通信中断异常"],
    "WARN": ["校准偏移提醒", "散热风扇转速异常"],
    "INFO": ["设备自检正常", "例行巡检完成"],
}
MAPPING = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "properties": {
            "lab_name": {"type": "keyword"},
            "region": {"type": "keyword"},
            "business_line": {"type": "keyword"},
            "level": {"type": "keyword"},
            "device_id": {"type": "keyword"},
            "message": {"type": "text"},
            "ts": {"type": "date"},
        }
    },
}


def generate() -> list[dict]:
    now = datetime.now(timezone.utc)
    docs: list[dict] = []
    for li, (lab, region, bl) in enumerate(LABS):
        for window_days, table in ((7, LAST7), (30, D8_30)):
            # 数据跨度**刻意小于**查询窗口，留出余量：
            # 「最近7天」的数据落在 0~4 天，「最近30天」那段落到 7~22 天。
            # 否则隔天重跑评估，最老的文档会滑出 now-7d 窗口，按构造的标准答案就漂移了
            # ——评估集的生命线是**可重复**，不能赌"刚好在灌完的那一刻查"。
            span_hours = 96 if window_days == 7 else 360
            base_hours = 0 if window_days == 7 else 7 * 24
            for level, n in table[region].items():
                for i in range(n):
                    ts = now - timedelta(hours=base_hours + (i + li) % span_hours)
                    docs.append(
                        {
                            "lab_name": lab,
                            "region": region,
                            "business_line": bl,
                            "level": level,
                            "device_id": f"DEV-{region[:1]}{li}-{i % 12:02d}",
                            "message": MSG[level][i % len(MSG[level])],
                            "ts": ts.isoformat(),
                        }
                    )
    return docs


def _first_msg_count(n: int) -> int:
    """message 按 i % 2 在两条 ERROR 文案间交替，第一条（温度超限）出现 ⌈n/2⌉ 次。"""
    return (n + 1) // 2


def expected() -> str:
    """打印按构造的标准答案（评估集直接抄这里）。"""
    err7 = {r: LAST7[r]["ERROR"] for r in LAST7}
    err30 = {r: LAST7[r]["ERROR"] + D8_30[r]["ERROR"] for r in LAST7}
    temp7 = sum(_first_msg_count(LAST7[r]["ERROR"]) for r in LAST7)
    lines = ["# 标准答案（按构造）", f"# 各区域 ERROR 最近7天: {err7}", f"# 各区域 ERROR 最近30天: {err30}",
             f"# 全部 ERROR 最近30天: {sum(err30.values())}",
             f"# 温度超限告警 最近7天: {temp7}（ERROR 文案按 i%2 交替，首条=温度超限）"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="灌 ES/OpenSearch 设备日志演示数据")
    parser.add_argument("--only-print", action="store_true", help="只打印标准答案")
    parser.add_argument(
        "--target", choices=["es", "ppl", "both"], default="es",
        help="灌到哪个集群：es=Elasticsearch(ES__HOST)、ppl=OpenSearch(ES__PPL_HOST)、both=两个都灌",
    )
    args = parser.parse_args(argv)

    docs = generate()
    print(f"生成 {len(docs)} 条事件")
    print(expected())
    if args.only_print:
        return 0

    from nl2sql.config import get_settings
    from nl2sql.es_backend import build_es_backend, resolve_index

    settings = get_settings()
    modes = ["es", "ppl"] if args.target == "both" else [args.target]

    rc = 0
    for mode in modes:
        rc |= _seed_one(settings, mode, docs, resolve_index(settings, mode), build_es_backend)
    return rc


def _seed_one(settings, mode: str, docs: list[dict], index: str, build_es_backend) -> int:
    """把演示数据灌进指定集群（重建索引，幂等）。"""
    label = "Elasticsearch (DSL)" if mode == "es" else "OpenSearch (PPL)"
    backend = build_es_backend(settings, mode=mode)
    if backend is None:
        hint = "ES__ENABLED/ES__HOST" if mode == "es" else "ES__PPL_ENABLED/ES__PPL_HOST"
        print(f"\n[{label}] 未配置（{hint}），跳过")
        return 2
    ok, info = backend.ping()
    if not ok:
        print(f"\n[{label}] 连接失败: {info}")
        print("  检查：① 控制台『安全配置 → 公网访问白名单』是否放行本机出口 IP；"
              "② 阿里云 ES 公网入口是 http 明文，写 https 会 TLS 握手失败")
        return 1
    print(f"\n[{label}] 已连接，版本 {info}，索引 {index}")

    # 重建索引（演示数据，幂等）
    backend._client.delete(f"/{index}", headers=backend._headers())  # 404 也无所谓
    r = backend._client.put(f"/{index}", json=MAPPING, headers=backend._headers())
    r.raise_for_status()

    # _bulk 灌数据（NDJSON）
    lines: list[str] = []
    for d in docs:
        lines.append(json.dumps({"index": {"_index": index}}))
        lines.append(json.dumps(d, ensure_ascii=False))
    body = "\n".join(lines) + "\n"
    resp = backend._client.post("/_bulk", content=body,
                                headers={"Content-Type": "application/x-ndjson"})
    resp.raise_for_status()
    if resp.json().get("errors"):
        print(f"[{label}] 部分文档写入失败，请检查响应")
        return 1
    backend._client.post(f"/{index}/_refresh", headers=backend._headers())
    print(f"[{label}] 已写入 {len(docs)} 条到索引 {index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
