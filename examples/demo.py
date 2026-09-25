"""端到端演示：需要真实 LLM（.env 中 LLM__*）与真实数据库（.env 中 DB__DSN）。

运行：
    python examples/demo.py

前置：
  1) 在 .env 填好 LLM__BASE_URL / LLM__API_KEY / LLM__MODEL 与 DB__DSN；
  2) 用 `python examples/setup_dev_db.py` 在目标库建示例表（orders / users）并灌数据。

演示覆盖三种最终来源：
  - source=llm             ：LLM 生成且通过校验 + 预检
  - source=fallback_template：LLM 幻觉字段被拦下，回退到最相关示例 SQL
  - source=fallback_generic ：检索无命中且生成失败，走全量 schema 兜底
"""
from __future__ import annotations

import os
import sys

# 让 examples/ 与 nl2sql/ 都可被导入（直接运行脚本时补上项目根目录）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from nl2sql.config import get_settings, setup_logging
from nl2sql.db import build_db
from nl2sql.llm import LLMClient, build_llm
from nl2sql.models import ResultSource
from nl2sql.pipeline import Text2SQLPipeline

from examples.schema import build_glossary, build_registry, build_store


class _AlwaysFailingLLM(LLMClient):
    """仅用于演示 fallback_generic：永远返回非法 SQL，逼出通用回退分支。"""

    def generate(self, prompt: str, system: str | None = None) -> str:
        return "SELECT not_a_real_column FROM no_such_table"


def _print_result(q: str, res, cols, rows) -> None:
    print(f"\n{'=' * 64}")
    print(f"[问题] {q}")
    print(f"[来源] {res.source.value}   error={res.error}")
    print(f"[SQL ] {res.sql}")
    print(f"[执行] columns={cols}  rows={rows}")


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log.level, settings.log.fmt)

    registry = build_registry(settings.db.dialect)
    store = build_store()
    glossary = build_glossary()
    llm = build_llm(settings.llm)
    db = build_db(settings.db, registry)

    pipeline = Text2SQLPipeline(
        registry=registry,
        store=store,
        llm=llm,
        db=db,
        glossary=glossary,
        top_k=settings.retrieval.top_k,
        min_score=settings.retrieval.min_score,
        max_retry=settings.pipeline.max_retry,
    )

    print("\n########## 主链路演示 ##########")
    for q in [
        "昨天有多少订单",          # 检索命中 -> LLM 成功 -> source=llm
        "昨天GMV是多少",           # 检索命中 -> LLM 幻觉字段 -> 回退示例 -> source=fallback_template
        "列出所有用户的姓名和地区",  # 维度标签命中 -> LLM 成功 -> source=llm
    ]:
        res, cols, rows = pipeline.query(q)
        _print_result(q, res, cols, rows)

    print("\n\n########## fallback_generic 演示（检索无命中 + 生成失败）##########")
    failing_pipeline = Text2SQLPipeline(
        registry=registry,
        store=store,
        llm=_AlwaysFailingLLM(),
        db=db,
        glossary=glossary,
        top_k=settings.retrieval.top_k,
        min_score=settings.retrieval.min_score,
        max_retry=settings.pipeline.max_retry,
    )
    res, cols, rows = failing_pipeline.query("随便问点什么奇怪的东西 xyz")
    _print_result("随便问点什么奇怪的东西 xyz", res, cols, rows)
    assert res.source == ResultSource.FALLBACK_GENERIC
    print("\n[断言] fallback_generic 已触发 ✓")


if __name__ == "__main__":
    main()
