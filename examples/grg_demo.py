"""计量检测智能问数 · 端到端演示（真实 LLM + 真实数据库）。

运行：
    python examples/grg_demo.py

前置：
  1) 在 .env 填好 LLM__* 与 DB__DSN；
  2) 用 `python examples/setup_dev_db.py` 建 9 张计量检测示例表并灌数据。

演示覆盖：
  1) 单轮语义映射：华东区上个月可靠性试验的准时完成率
     - 同义词展开、指标解析、区域/时间实体抽取、注入口径 Glossary、真实执行
  2) 多轮继承：那华南区呢？
     - 继承 {业务线=可靠性, 指标=准时率, 时间=上个月}，仅替换区域=华南
  3) 歧义澄清：那个做环境的实验室利用率怎么样
     - 触发 ambiguous 同义词，返回澄清问题，不进入生成
  4) 口径说明：每条结果附"指标=口径，数据来源=表"的可审计说明
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from nl2sql.config import get_settings, setup_logging
from nl2sql.db import build_db
from nl2sql.llm import build_llm
from nl2sql.pipeline import Text2SQLPipeline

from examples.grg_engine import GRGQueryEngine
from examples.grg_schema import build_registry, build_semantic_layer, build_store


def _print_mapped(mapped) -> None:
    print("  [语义映射]")
    for r in mapped.reasons:
        print(f"    - {r}")
    if mapped.metric:
        print(f"    - 解析指标: {mapped.metric.name} ({mapped.metric.level})")
        print(f"    - 口径: {mapped.metric.definition}")
    print(f"    - 实体: {mapped.entities}")


def _print_result(out: dict) -> None:
    res = out["result"]
    print(f"  [来源] {res.source.value}   error={res.error}")
    print(f"  [SQL ] {res.sql}")
    print(f"  [执行] columns={out['cols']}  rows={out['rows']}")
    # 口径说明（可审计）
    if out["mapped"].metric:
        m = out["mapped"].metric
        print(f"  [口径说明] {m.name} = {m.definition}；数据来源: {','.join(m.source_tables)}")


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log.level, settings.log.fmt)

    registry = build_registry(settings.db.dialect)
    store = build_store()
    layer = build_semantic_layer()

    # 真实 LLM（.env 的 LLM__*）+ 真实数据库（.env 的 DB__DSN）
    llm = build_llm(settings.llm)
    db = build_db(settings.db, registry)

    pipeline = Text2SQLPipeline(
        registry=registry,
        store=store,
        llm=llm,
        db=db,
        top_k=settings.retrieval.top_k,
        min_score=settings.retrieval.min_score,
        max_retry=settings.pipeline.max_retry,
    )
    engine = GRGQueryEngine(pipeline, layer)

    print("\n########## 1) 单轮语义映射 ##########")
    out = engine.ask("华东区上个月可靠性试验的准时完成率是多少")
    print(f"\n[问题] 华东区上个月可靠性试验的准时完成率是多少")
    _print_mapped(out["mapped"])
    _print_result(out)

    print("\n\n########## 2) 多轮继承（那华南区呢？） ##########")
    out2 = engine.ask("那华南区呢？")
    print(f"\n[问题] 那华南区呢？")
    _print_mapped(out2["mapped"])
    _print_result(out2)
    east_rate = out["rows"][0][0] if out.get("rows") else None
    south_rate = out2["rows"][0][0] if out2.get("rows") else None
    print(f"  [对比] 华东 on_time_rate={east_rate} vs 华南 on_time_rate={south_rate}"
          f"（上下文继承 + 区域替换生效）")

    print("\n\n########## 3) 歧义澄清 ##########")
    engine.reset_context()
    out3 = engine.ask("那个做环境的实验室利用率怎么样")
    print(f"\n[问题] 那个做环境的实验室利用率怎么样")
    print(f"  [类型] {out3['type']}")
    print(f"  [澄清] {out3['message']}")

    print("\n\n########## 4) 集成电路一次通过率（跨业务线） ##########")
    out4 = engine.ask("集成电路测试的检测一次通过率")
    print(f"\n[问题] 集成电路测试的检测一次通过率")
    _print_mapped(out4["mapped"])
    _print_result(out4)


if __name__ == "__main__":
    main()
