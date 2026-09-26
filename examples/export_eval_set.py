"""导出「三语言评测集」：SQL / ES DSL / OpenSearch PPL，含标准答案与当次实测结果。

为什么要有这个脚本：
  三套引擎的评估集本来就散在三个地方——SQL 在 `evals/cases.py`（65 条，标准答案是标准 SQL，
  评测时真跑一遍按值比对）、DSL/PPL 在 `examples/es_eval.py::ES_CASES`（9 条，标准答案按构造写死）。
  面试/演示时要一份"能直接看、能对答案"的清单，人工翻三个文件太费劲，故统一导出成一份 Markdown。

用法：
    python examples/export_eval_set.py                # 只导出标准答案（不连任何集群）
    python examples/export_eval_set.py --live         # 额外连真机跑 DSL/PPL，把实测值也写进文档
    python examples/export_eval_set.py --out EVAL_SET.md

三个引擎的比对口径（都是 execution accuracy，只比**值**不比 SQL 字符串）：
    SQL  : 标准 SQL 与系统 SQL 在同一个库上各跑一遍，按值比对（浮点 4 位归一、多行按集合）
    DSL  : 与"灌数时按构造写死的常量"比对（examples/setup_es_demo.py 里生成时就确定）
    PPL  : 同一批常量；PPL 是 OpenSearch 专有语言，普通 ES 上会降级为"仅编译"
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from examples.dump_expected_answers import (  # noqa: E402  复用口径说明/归一/格式化，避免两处漂移
    _fmt_rows,
    _fmt_value,
    _known_failures,
    _load_cases,
    _load_dsn,
    _norm,
    _note_for,
)
from examples.es_eval import ES_CASES  # noqa: E402

GROUPS_ORDER = [
    "指标-准时率", "指标-一次通过率", "指标-收入", "指标-周期", "指标-利用率",
    "总量", "分组", "排名", "多轮", "澄清", "文档RAG", "安全", "经营", "边界",
]


def _run_es_live(mode: str) -> tuple[dict, str]:
    """连真机跑一遍 DSL/PPL，返回 ({case_id: 实测展示串}, 结论文字)。"""
    from nl2sql.config import get_settings
    from nl2sql.es_backend import build_es_backend, resolve_index
    from examples.es_engine import EsQueryEngine

    settings = get_settings()
    backend = build_es_backend(settings, mode=mode)
    label = "Elasticsearch（DSL）" if mode == "es" else "OpenSearch（PPL）"
    if backend is None:
        return {}, f"{label} 未配置，跳过实测"
    ok, info = backend.ping()
    if not ok:
        return {}, f"{label} 连接失败：{str(info)[:120]}"
    eng = EsQueryEngine(backend, index=resolve_index(settings, mode))

    measured: dict[str, str] = {}
    passed = 0
    for c in ES_CASES:
        out = eng.ask(c["question"])
        if mode == "ppl":
            ppl = out.get("ppl") or {}
            if ppl.get("status") != "executed":
                measured[c["id"]] = f"未执行（{str(ppl.get('status_detail') or ppl.get('status'))[:40]}）"
                continue
            rows = ppl.get("rows") or []
        else:
            rows = out.get("rows") or []
        measured[c["id"]] = _fmt_es(rows, c["kind"])
        passed += bool(_es_ok(c, rows))
    return measured, f"{label} v{info}：{passed}/{len(ES_CASES)} 通过"


def _fmt_es(rows: list, kind: str) -> str:
    """按用例类型取展示列（必须与 compare 的取值口径一致）：
    scalar 取值列、rows 展示分布、top 取**名称列**（早先这里统一取末列，
    把「告警最多的区域」显示成了数字 60 —— 判定是对的，展示错了）。"""
    if not rows:
        return "（空）"
    if kind == "top":
        return str(rows[0][0])
    if len(rows) == 1 and len(rows[0]) == 1:
        return str(rows[0][0])
    if len(rows) == 1:
        return str(rows[0][-1])
    parts = [f"{r[0]} → {r[-1]}" for r in rows[:4]]
    return "；".join(parts) + ("…" if len(rows) > 4 else "")


def _es_ok(case: dict, rows: list) -> bool:
    """与 examples/es_eval.py::compare 同口径。"""
    if not rows:
        return False
    if case["kind"] == "scalar":
        return rows[0][-1] == case["expected"]
    if case["kind"] == "rows":
        return {r[0]: r[-1] for r in rows} == case["expected"]
    if case["kind"] == "top":
        return rows[0][0] == case["expected"]
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出三语言评测集（SQL / DSL / PPL）")
    ap.add_argument("--live", action="store_true", help="连真机跑一遍 DSL/PPL 并写入实测值")
    ap.add_argument("--out", default="EVAL_SET.md")
    args = ap.parse_args(argv)

    cases = _load_cases()

    # ---- SQL 侧：真库执行标准 SQL 取期望值 ----
    import psycopg

    expected: dict = {}
    with psycopg.connect(_load_dsn(), connect_timeout=20) as conn:
        for c in cases:
            sql = c.get("truth_sql")
            if not sql:
                continue
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
            if c.get("compare", "value") == "rows":
                nr = sorted(tuple(_norm(v) for v in r) for r in rows)
                expected[c["id"]] = {"mode": "rows", "rows": [[_fmt_value(v) for v in r] for r in nr]}
            else:
                expected[c["id"]] = {
                    "mode": "value",
                    "value": _norm(rows[0][0]) if rows and rows[0] else None,
                }

    es_live: dict[str, str] = {}
    es_notes: list[str] = []
    if args.live:
        for mode in ("es", "ppl"):
            got, note = _run_es_live(mode)
            es_notes.append(note)
            for cid, v in got.items():
                es_live[f"{mode}:{cid}"] = v

    failures = {cid: q for cid, q, _ in _known_failures()}

    L: list[str] = []
    w = L.append
    w("# 三语言评测集（SQL · ES DSL · OpenSearch PPL）\n")
    w("> 三套执行引擎、三份数据集，**统一按 execution accuracy 比对**——只比结果**值**，")
    w("> 不比 SQL/PPL 字符串长得像不像（NL2SQL 领域评测的标准做法）。\n>")
    w("> 标准答案的两种来源：")
    w("> ① **SQL 侧**：标准答案是评估集自带的 ground-truth SQL，导出时在真库上跑一遍取期望值；")
    w("> ② **DSL/PPL 侧**：标准答案是灌数时**按构造写死的常量**（`examples/setup_es_demo.py`），")
    w("> 数据就是照这些数字生成的，所以答案精确已知且可复现。\n")
    w(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M}　|　执行：`python examples/export_eval_set.py --live`\n")
    w("## 0. 总览\n")
    w("| 引擎 | 数据集 | 条数 | 标准答案来源 | 比对方式 |")
    w("|---|---|---|---|---|")
    w(f"| 🔢 SQL（PostgreSQL） | `evals/cases.py` | {len(cases)} | ground-truth SQL 真跑 | 按值 / 行集比对 |")
    w(f"| 🔎 DSL（Elasticsearch） | `examples/es_eval.py::ES_CASES` | {len(ES_CASES)} | 按构造常量 | 按值 / 分布比对 |")
    w(f"| 🧭 PPL（OpenSearch） | 同一批 ES_CASES | {len(ES_CASES)} | 按构造常量 | 同上（走 `_plugins/_ppl`） |")
    w("")
    if es_notes:
        w("**当次实测**：" + "；".join(es_notes) + "\n")
    w("三套引擎共用同一份中间表示（QueryIR）与同一套安全护栏——**换引擎不换安全等级**，")
    w("方言差异（时间、字符集）全部在编译层消化，详见 README §9.1.1。\n")
    w("---\n")

    # ---------------- 一、SQL ----------------
    groups: dict[str, list] = {}
    for c in cases:
        groups.setdefault(c["category"], []).append(c)
    w(f"## 一、SQL 评估集：{len(cases)} 条\n")
    w("跑法 `python examples/eval_run.py`；浮点按 4 位小数归一，多行按排序后的集合比对。\n")
    for cat in GROUPS_ORDER:
        items = groups.get(cat)
        if not items:
            continue
        w(f"### {cat}（{len(items)} 条）\n")
        w("| ID | 问句 | 标准答案 | 口径说明 |")
        w("|---|---|---|---|")
        for c in items:
            ex = expected.get(c["id"])
            if ex and ex["mode"] == "rows":
                ans = _fmt_rows([tuple(r) for r in ex["rows"]])
            elif ex:
                ans = f"**{_fmt_value(ex['value'])}**"
            else:
                ans = "语义类（见下文）"
            mark = " ⚠️*已知失败*" if c["id"] in failures else ""
            w(f"| {c['id']}{mark} | {c['question']} | {ans} | {_note_for(c)} |")
        w("")

    # ---------------- 二、DSL / PPL ----------------
    w("---\n")
    w(f"## 二、DSL / PPL 评估集：{len(ES_CASES)} 条（设备日志域）\n")
    w("跑法：`python examples/es_eval.py --target both`（先 `setup_es_demo.py --target both` 灌数）。\n")
    w("> 注：多行结果**行序不计入判定**（按集合比对），所以 DSL 与 PPL 的分布行序不同属正常。\n")
    if args.live:
        w("| ID | 问句 | 标准答案 | DSL 实测 | PPL 实测 |")
        w("|---|---|---|---|---|")
        for c in ES_CASES:
            exp = c["expected"]
            exp_s = str(exp) if not isinstance(exp, dict) else "；".join(f"{k} → {v}" for k, v in exp.items())
            w(f"| {c['id']} | {c['question']} | {exp_s} | "
              f"{es_live.get('es:' + c['id'], '—')} | {es_live.get('ppl:' + c['id'], '—')} |")
    else:
        w("| ID | 问句 | 标准答案 |")
        w("|---|---|---|")
        for c in ES_CASES:
            exp = c["expected"]
            exp_s = str(exp) if not isinstance(exp, dict) else "；".join(f"{k} → {v}" for k, v in exp.items())
            w(f"| {c['id']} | {c['question']} | {exp_s} |")
    w("")

    # ---------------- 三、方言对照 ----------------
    w("---\n")
    w("## 三、同一份 IR 的两种方言（编译产物对照）\n")
    w("SQL 走的是另一条链路（LLM 生成 + sqlglot 校验），下面看 ES DSL 与 PPL 如何从**同一份 QueryIR** 编译出来：\n")
    w("| 问句 | Elasticsearch DSL | OpenSearch PPL |")
    w("|---|---|---|")
    try:
        from nl2sql.config import get_settings
        from nl2sql.es_backend import resolve_index
        from examples.es_engine import EsQueryEngine
        from nl2sql.es_backend import ElasticsearchBackend, build_es_backend

        s = get_settings()
        backend = build_es_backend(s, mode="es") or ElasticsearchBackend(host="http://localhost:9200")
        eng = EsQueryEngine(backend, index=resolve_index(s, "es"))
        for c in ES_CASES[:4]:
            parsed, gb, ob, od, lm = eng._parse(c["question"])
            ir = eng._assemble(parsed, gb, ob, od, lm)
            import json as _json
            dsl_json = _json.dumps(ir.to_es_dsl(), ensure_ascii=False, separators=(",", ":"))
            dsl = "`" + (dsl_json[:260] + ("…" if len(dsl_json) > 260 else "")).replace("|", "\\|") + "`"
            ppl = "`" + ir.to_ppl().replace("|", "\\|") + "`"
            w(f"| {c['question']} | {dsl} | {ppl} |")
    except Exception as e:  # noqa: BLE001
        w(f"（方言对照生成失败：{e}）")
    w("")

    # ---------------- 四、复现 ----------------
    w("---\n")
    w("## 四、复现命令\n")
    w("```bash")
    w("# SQL：65 条真实 LLM + 真实库（结果写 evals/last_report.json）")
    w("python examples/eval_run.py                       # 全量")
    w("python examples/eval_run.py --id G06 --id C06     # 指定用例")
    w("python examples/eval_run.py --min-accuracy 0.9    # 接 CI，低于阈值退出码 1")
    w("")
    w("# DSL / PPL：9 条真机（结果写 evals/last_es_report*.json）")
    w("python examples/setup_es_demo.py --target both    # 灌数（两套集群各 1222 条）")
    w("python examples/es_eval.py --target both          # 双份成绩单")
    w("")
    w("# 离线单测（不连任何服务）：IR/编译器/安全护栏/入口")
    w("pytest -q")
    w("```")
    w("")
    w("## 五、说明与已知失败\n")
    if failures:
        w("SQL 侧当前失败（属**模型服从性边界**，非数据问题）：\n")
        for cid, q in failures.items():
            w(f"- **{cid}** {q}")
        w("")
        w("这类反复出现的失败，项目采用**指标口径治理**（改 `definition`/`sql_hint`）而不是改提示词模板，")
        w("历史上已用该方法治过：合同金额口径、设备保有量口径、毛利率板块过滤位置、设备利用率是否剔除离线设备。\n")
    else:
        w("SQL 侧当前无失败用例。\n")
    out = ROOT / args.out
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"已导出：{out.relative_to(ROOT)}（{out.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
