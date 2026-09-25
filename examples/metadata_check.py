"""schema 变更检测：对比当前元数据与上次快照，把「变了什么」讲清楚。

用法：
    python examples/metadata_check.py            # 对比快照，打印变更报告
    python examples/metadata_check.py --save     # 保存当前结构为快照
    python examples/metadata_check.py --api http://meta.internal  # 从元数据中心拉取后对比

为什么需要它：
  上游加字段/改类型时，问数系统的校验白名单和 prompt 里的 schema 都是旧的——
  与其等 LLM 幻觉了新字段再排查，不如在 CI/定时任务里**主动发现**并告警。
  退出码：有变更为 1，无变更为 0 —— 可直接接 CI 卡口。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SNAPSHOT = Path(__file__).resolve().parents[1] / ".metadata_snapshot.json"


def _to_snapshot(tables) -> dict:
    return {
        "fingerprint": None,  # 由调用方填
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "tables": [
            {
                "name": t.name,
                "description": t.description,
                "columns": t.columns,
                "foreign_keys": [list(fk) for fk in t.foreign_keys],
                "sample_values": t.sample_values,
            }
            for t in tables
        ],
    }


def _from_snapshot(data: dict):
    from nl2sql.models import TableSchema

    return [
        TableSchema(
            name=t["name"],
            columns=dict(t["columns"]),
            description=t.get("description", ""),
            foreign_keys=[tuple(fk) for fk in t.get("foreign_keys") or []],
            sample_values=dict(t.get("sample_values") or {}),
        )
        for t in data["tables"]
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="schema 变更检测")
    parser.add_argument("--save", action="store_true", help="把当前结构保存为快照")
    parser.add_argument("--api", default="", help="临时指定元数据中心地址（覆盖 METADATA__BASE_URL）")
    args = parser.parse_args(argv)

    from nl2sql.config import get_settings
    from nl2sql.metadata import build_metadata_provider, diff_tables, fingerprint, format_diff

    settings = get_settings()
    if args.api:
        settings.metadata.base_url = args.api
        settings.metadata.provider = "api"

    provider = build_metadata_provider(settings)
    tables = provider.load()
    fp = fingerprint(tables)
    print(f"提供者: {type(provider).__name__} | 表数量: {len(tables)} | 指纹: {fp}")

    if args.save:
        snap = _to_snapshot(tables)
        snap["fingerprint"] = fp
        SNAPSHOT.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"快照已保存: {SNAPSHOT}")
        return 0

    if not SNAPSHOT.exists():
        print("尚无快照。首次使用请先执行: python examples/metadata_check.py --save")
        return 0

    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    if snap.get("fingerprint") == fp:
        print("无结构变更")
        return 0

    print(f"⚠️ schema 已变更: {snap.get('fingerprint')} -> {fp}")
    print(format_diff(diff_tables(_from_snapshot(snap), tables)))
    print("\n处理建议：核对以上变更是否需要更新口径/示例库/知识库语料，确认后执行 --save 固化")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
