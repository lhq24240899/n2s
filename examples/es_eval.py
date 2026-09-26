"""ES（第二执行引擎）的评估集：真集群 execution accuracy。

和 SQL 评估集（evals/）的区别与联系：
- SQL 评估集：标准答案是**标准 SQL**，与系统 SQL 各跑一遍按值比对；
- ES 评估集：标准答案是**按构造的常量**（`setup_es_demo.py` 里数据就是照这些数字生成的），
  直接比对系统返回的聚合值。

两者共同点：都不比对"字符串长得像不像"，只比对**执行结果对不对**。

用法：
    python examples/es_eval.py                    # 全量（需 ES__ENABLED=true 且集群可达）
    python examples/es_eval.py --min-accuracy 1.0 # 低于阈值退出码 1，可接 CI
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nl2sql.config import get_settings
from nl2sql.es_backend import build_es_backend, resolve_index
from examples.es_engine import EsQueryEngine

# kind:
#   scalar -> rows[0][-1] 等于 expected（单值，允许系统按 level 多分一层）
#   rows   -> {第一列: 末列} 等于 expected（分组分布）
#   top    -> rows[0][0] 等于 expected（排名问句的第一名）
ES_CASES: list[dict] = [
    {"id": "E01", "question": "华东区最近7天的ERROR告警有多少条",
     "kind": "scalar", "expected": 42},
    {"id": "E02", "question": "各区域最近7天的ERROR告警数量",
     "kind": "rows", "expected": {"华东": 42, "华南": 30, "华北": 25}},
    {"id": "E03", "question": "各区域最近30天的ERROR告警数量",
     "kind": "rows", "expected": {"华东": 60, "华南": 42, "华北": 35}},
    {"id": "E04", "question": "最近30天共有多少条ERROR告警",
     "kind": "scalar", "expected": 137},
    {"id": "E05", "question": "包含「温度超限」的告警最近7天有多少条",
     "kind": "scalar", "expected": 49},
    {"id": "E06", "question": "各实验室最近7天的ERROR告警数量",
     "kind": "rows", "expected": {
         "上海集成电路实验室": 42, "深圳可靠性实验室": 30, "北京电磁兼容实验室": 25}},
    {"id": "E07", "question": "告警最多的区域是哪个", "kind": "top", "expected": "华东"},
    {"id": "E08", "question": "告警最少的区域是哪个", "kind": "top", "expected": "华北"},
    {"id": "E09", "question": "最近7天各级别有多少条日志",
     "kind": "rows", "expected": {"ERROR": 97, "WARN": 145, "INFO": 470}},
]


def compare(kind: str, rows: list[tuple], expected) -> tuple[bool, str]:
    if not rows:
        return False, "系统返回空结果"
    if kind == "scalar":
        got = rows[0][-1]
        return got == expected, f"系统={got} 标准={expected}"
    if kind == "rows":
        got = {r[0]: r[-1] for r in rows}
        return got == expected, f"系统={got} 标准={expected}"
    if kind == "top":
        got = rows[0][0]
        return got == expected, f"系统={got} 标准={expected}"
    return False, f"未知 kind: {kind}"


def _run_mode(settings, mode: str, cases: list[dict], min_accuracy: float | None) -> int:
    """在指定引擎上跑一轮评估。

    mode="es"  -> 主指标是 **ES DSL 真执行**的结果（PPL 作为对照，可能仅编译）
    mode="ppl" -> 主指标是 **PPL 真执行**的结果（DSL 作为对照）

    同一份 QueryIR 编译出两种方言，所以两条链路跑的是同一批标准答案——
    谁的通过率低，就说明是那个方言的编译或执行出了问题。
    """
    label = "Elasticsearch（DSL）" if mode == "es" else "OpenSearch（PPL）"
    backend = build_es_backend(settings, mode=mode)
    if backend is None:
        hint = "ES__ENABLED=true 与 ES__HOST" if mode == "es" else "ES__PPL_ENABLED=true 与 ES__PPL_HOST"
        print(f"[{label}] 未启用：请设置 {hint}")
        return 2
    ok, info = backend.ping()
    if not ok:
        print(f"[{label}] 连接失败: {info}")
        print("  检查：控制台『安全配置 → 公网访问白名单』；ES__HOST 的 scheme（阿里云公网入口是 http）")
        return 2
    index = resolve_index(settings, mode)
    print(f"\n[{label}] 已连接，版本 {info}，索引 {index}\n")

    eng = EsQueryEngine(backend, index=index)
    other = "ppl" if mode == "es" else "es"

    records, passed, other_pass, other_total = [], 0, 0, 0
    for c in cases:
        t0 = time.time()
        out = eng.ask(c["question"])
        dt = (time.time() - t0) * 1000

        if mode == "es":
            other_info = out.get("ppl") or {}
            other_rows = other_info.get("rows") or []
            other_ran = other_info.get("status") == "executed"
            other_detail = other_info.get("status_detail") or other_info.get("status")
        else:
            other_info = {"es_dsl": out.get("es_dsl")}
            other_rows = out.get("rows") or []
            other_ran = out.get("type") == "result"
            other_detail = None

        if out.get("type") != "result":
            ok_case, detail = False, f"非结果返回: {out.get('message') or out.get('type')}"
            other_ran = False
        elif mode == "es":
            ok_case, detail = compare(c["kind"], out["rows"], c["expected"])
        else:
            ppl_info = out.get("ppl") or {}
            if ppl_info.get("status") != "executed":
                ok_case = False
                detail = f"PPL 未真执行: {ppl_info.get('status_detail') or ppl_info.get('status')}"
            else:
                ok_case, detail = compare(c["kind"], ppl_info.get("rows") or [], c["expected"])

        other_ok = None
        if other_ran:
            other_total += 1
            other_ok, _ = compare(c["kind"], other_rows, c["expected"])
            other_pass += bool(other_ok)

        passed += bool(ok_case)
        records.append({
            "id": c["id"], "question": c["question"], "ok": ok_case, "detail": detail,
            "other_ok": other_ok, "other_detail": other_detail, "ms": round(dt, 1),
            "es_dsl": out.get("es_dsl"), "ppl": out.get("ppl"), "rows": out.get("rows"),
        })
        mark = "?" if other_ok is None else ("P" if other_ok else "X")
        other_name = "ppl" if mode == "es" else "dsl"
        print(f"[{'PASS' if ok_case else 'FAIL'}|{other_name}:{mark}] {c['id']} {c['question']} -> {detail}")

    total = len(cases)
    acc = passed / total if total else 0.0
    other_name = "PPL" if mode == "es" else "ES DSL"
    print(f"\n{'ES DSL' if mode == 'es' else 'PPL    '} 通过 {passed}/{total} = {acc:.1%}   ← 本档主指标")
    if other_total:
        print(f"{other_name:7s} 通过 {other_pass}/{other_total} = {other_pass / other_total:.1%}"
              f"（{total - other_total} 条未真执行）")
    else:
        print(f"{other_name:7s} 本轮未真执行（端点不支持该方言时降级为「仅编译」）")

    out_json = Path("evals/last_es_report.json" if mode == "es" else "evals/last_es_report_ppl.json")
    Path("evals").mkdir(exist_ok=True)
    out_json.write_text(json.dumps(
        {"engine": mode, "version": info, "index": index, "passed": passed, "total": total,
         "accuracy": acc, "other_passed": other_pass, "other_total": other_total,
         "records": records}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"报告: {out_json}")

    if min_accuracy is not None and acc < min_accuracy:
        print(f"低于阈值 {min_accuracy:.0%}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ES DSL / OpenSearch PPL 第二执行引擎评估")
    parser.add_argument("--min-accuracy", type=float, default=None)
    parser.add_argument("--id", action="append", default=[])
    parser.add_argument(
        "--target", choices=["es", "ppl", "both"], default="es",
        help="跑哪个引擎：es=Elasticsearch(DSL)、ppl=OpenSearch(PPL)、both=两档各跑一轮（双份成绩单）",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    cases = [c for c in ES_CASES if not args.id or c["id"] in args.id]
    modes = ["es", "ppl"] if args.target == "both" else [args.target]

    rc = 0
    for mode in modes:
        rc |= _run_mode(settings, mode, cases, args.min_accuracy)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
