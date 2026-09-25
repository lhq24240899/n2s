"""问数评估：真实 LLM + 真实库，跑评估集并输出通过率（execution accuracy）。

用法：
    python examples/eval_run.py                       # 全量
    python examples/eval_run.py --category 分组       # 只跑某一类
    python examples/eval_run.py --id A01 --id F02     # 只跑指定用例
    python examples/eval_run.py --min-accuracy 0.9    # 低于阈值退出码为 1（可接 CI）

设计：
- 每条用例的标准答案是**标准 SQL**：与系统生成的 SQL 在同一个库上各跑一遍，按值比对。
- 多轮用例（同 `session`）按列表顺序在同一个会话里执行，验证上下文继承。
- 报告写入 evals/last_report.json，包含每条用例的类型、延迟、失败原因——
  失败明细就是下一轮优化的工作清单。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.cases import CASES
from evals.harness import evaluate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="问数评估（真实 LLM + 真实库）")
    parser.add_argument("--category", action="append", help="只跑指定类别（可多次）")
    parser.add_argument("--id", action="append", help="只跑指定用例 ID（可多次）")
    parser.add_argument("--limit", type=int, default=0, help="最多跑 N 条（调试用）")
    parser.add_argument("--min-accuracy", type=float, default=0.8, help="通过率阈值")
    parser.add_argument("--tag", default="", help="报告备注（如'加 PPL 前'）")
    args = parser.parse_args(argv)

    cases = CASES
    if args.category:
        cases = [c for c in cases if c["category"] in args.category]
    if args.id:
        cases = [c for c in cases if c["id"] in args.id]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("没有匹配的用例")
        return 2

    from nl2sql.auth import Principal, Role
    from nl2sql.bootstrap import build_service
    from nl2sql.config import get_settings

    settings = get_settings()
    service, _ctx = build_service(settings)
    principal = Principal.of("eval", Role.ANALYST)

    import psycopg

    truth_conn = psycopg.connect(settings.db.dsn, connect_timeout=20, autocommit=True)
    truth_cur = truth_conn.cursor()

    records: list[dict] = []
    started = time.perf_counter()
    for case in cases:
        qid, question = case["id"], case["question"]
        # 同 session 的用例共享会话（多轮）；否则各用独立会话，互不污染
        sid = case.get("session") or f"eval:{qid}"
        rec: dict = {"id": qid, "category": case["category"], "question": question}
        t0 = time.perf_counter()
        try:
            payload = service.ask(question, principal, session_id=sid)
            truth_cols, truth_rows = None, None
            if case.get("truth_sql"):
                truth_cur.execute(case["truth_sql"])
                truth_cols = [d[0] for d in truth_cur.description]
                truth_rows = truth_cur.fetchall()
            ok, detail = evaluate(case, payload, truth_cols, truth_rows)
            rec.update(
                ok=ok, detail=detail, type=payload.get("type"),
                latency_ms=payload.get("latency_ms"), sql=payload.get("sql"),
            )
        except Exception as e:  # noqa: BLE001 - 评估器不能被单条失败打断
            ok, detail = False, f"异常: {e}"
            rec.update(ok=ok, detail=detail, type="exception")
        rec["sec"] = round(time.perf_counter() - t0, 1)
        records.append(rec)
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {qid:>4} {case['category']:<8} {rec.get('detail', '')[:110]}")

    total = len(records)
    passed = sum(1 for r in records if r["ok"])
    accuracy = passed / total if total else 0.0
    by_cat: dict[str, dict] = {}
    for r in records:
        c = by_cat.setdefault(r["category"], {"total": 0, "passed": 0})
        c["total"] += 1
        c["passed"] += int(r["ok"])

    print("\n===== 评估结果 =====")
    print(f"通过 {passed}/{total} = {accuracy:.1%}（阈值 {args.min_accuracy:.0%}）")
    for cat, s in sorted(by_cat.items()):
        print(f"  {cat:<10} {s['passed']}/{s['total']}")

    report = {
        "tag": args.tag,
        "accuracy": round(accuracy, 4),
        "passed": passed,
        "total": total,
        "elapsed_sec": round(time.perf_counter() - started, 1),
        "by_category": by_cat,
        "records": records,
    }
    out = Path(__file__).resolve().parents[1] / "evals" / "last_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入 {out}")

    truth_conn.close()
    return 0 if accuracy >= args.min_accuracy else 1


if __name__ == "__main__":
    raise SystemExit(main())
