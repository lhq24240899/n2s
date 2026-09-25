"""灌检测报告文本数据到 ES/OpenSearch：支撑 **全文检索** 与知识库 RAG 语料。

与设备日志的区别：日志是**事件流水**（计数/分组），报告是**文本资产**（要能按内容搜）。
同一套 IR 编译器目前只覆盖前者——报告检索走 ES 的 match/全文能力，或直接喂知识库 RAG。

同样遵守「维度真实、度量按构造」：检测项目/结论用真实业务词表，
但各类的**数量是构造出来的确定值**（脚本打印标准答案），不能靠随机撒点。

用法：
    python examples/seed_report_docs.py                 # 建索引 + 灌 2000 份 + 打印标准答案
    python examples/seed_report_docs.py --only-print    # 只看标准答案
    python examples/seed_report_docs.py --num 500       # 指定份数
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

INDEX = "inspection_reports"

# ---- 真实业务词表（维度真实） ----
LABS = ["广州计量实验室", "深圳可靠性实验室", "北京电磁兼容实验室",
        "上海集成电路实验室", "无锡可靠性实验室"]
TEST_ITEMS = [
    "挥发性有机化合物", "重金属含量", "电磁兼容性", "环境可靠性",
    "食品添加剂", "微生物限度", "RoHS 有害物质", "盐雾试验",
]
CONCLUSIONS = ["合格", "不合格", "符合", "不符合", "N.D."]
SAMPLES = ["饮用水", "土壤", "电子产品整机", "汽车零部件", "食品包装材料",
           "医用耗材", "锂电池组", "通信设备"]

MAPPING = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "properties": {
            "report_id": {"type": "keyword"},
            "lab_name": {"type": "keyword"},
            "sample_name": {"type": "text"},
            "test_item": {"type": "text"},
            "conclusion_text": {"type": "keyword"},
            "report_date": {"type": "date"},
        }
    },
}

# 结论分布：按 i % 20 确定性分配（不用 random，保证数量可复算）
# 合格 14/20=70%, 不合格 2/20=10%, 符合 2/20=10%, 不符合 1/20=5%, N.D. 1/20=5%
_BUCKETS = (
    ["合格"] * 14 + ["不合格"] * 2 + ["符合"] * 2 + ["不符合"] * 1 + ["N.D."] * 1
)


def generate(num: int = 2000) -> list[dict]:
    today = date.today()
    docs: list[dict] = []
    for i in range(num):
        lab = LABS[i % len(LABS)]
        docs.append({
            "report_id": f"RPT-{today.year}-{10000 + i}",
            "lab_name": lab,
            "sample_name": SAMPLES[(i // len(LABS)) % len(SAMPLES)],
            "test_item": TEST_ITEMS[(i // 3) % len(TEST_ITEMS)],
            "conclusion_text": _BUCKETS[i % len(_BUCKETS)],
            "report_date": (today - timedelta(days=(i * 7) % 365)).isoformat(),
        })
    return docs


def expected(docs: list[dict]) -> str:
    """打印按构造的标准答案（全文检索类评估直接抄这里）。"""
    from collections import Counter

    by_concl = Counter(d["conclusion_text"] for d in docs)
    by_item = Counter(d["test_item"] for d in docs)
    by_lab = Counter(d["lab_name"] for d in docs)
    lines = ["# 标准答案（按构造）", f"# 总数: {len(docs)}"]
    lines.append("# 各结论数量: " + ", ".join(f"{k}={by_concl[k]}" for k in CONCLUSIONS))
    lines.append("# 各检测项目数量: " + ", ".join(f"{k}={by_item[k]}" for k in TEST_ITEMS))
    lines.append("# 各实验室数量: " + ", ".join(f"{k}={by_lab[k]}" for k in LABS))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="灌检测报告文本到 ES/OpenSearch")
    parser.add_argument("--only-print", action="store_true")
    parser.add_argument("--num", type=int, default=2000)
    args = parser.parse_args(argv)

    docs = generate(args.num)
    print(expected(docs))
    if args.only_print:
        return 0

    from nl2sql.config import get_settings
    from nl2sql.es_backend import build_es_backend

    backend = build_es_backend(get_settings())
    if backend is None:
        print("未启用 ES（ES__ENABLED / ES__HOST）")
        return 2
    ok, info = backend.ping()
    if not ok:
        print(f"连接失败: {info}")
        return 1
    print(f"已连接 {info}，写入索引 {INDEX}")

    c = backend._client
    h = backend._headers()
    c.delete(f"/{INDEX}", headers=h)
    r = c.put(f"/{INDEX}", json=MAPPING, headers=h)
    r.raise_for_status()

    lines: list[str] = []
    for d in docs:
        lines.append(json.dumps({"index": {"_index": INDEX}}))
        lines.append(json.dumps(d, ensure_ascii=False))
    resp = c.post("/_bulk", content="\n".join(lines) + "\n",
                  headers={"Content-Type": "application/x-ndjson"})
    resp.raise_for_status()
    if resp.json().get("errors"):
        print("部分文档写入失败")
        return 1
    c.post(f"/{INDEX}/_refresh", headers=h)
    print(f"已写入 {len(docs)} 份报告到 {INDEX}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
