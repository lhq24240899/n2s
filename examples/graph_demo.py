"""手写 pipeline vs LangGraph 编排：同一批问题、同一批组件，对照两者的流程与结果。

运行：
    python examples/graph_demo.py

看点（面试可直接演示）：
1. 同一问题两条编排的**结果一致**（组件复用，只是编排不同）；
2. 图版会打印**逐节点流程轨迹**，一眼看清"哪一层出了什么问题"；
3. 构造一个必然为空的问题，看 **Critic 反思节点**如何打回、重生成、最后回退；
4. `--llm-critic` 打开 LLM 复核（默认只用零成本的规则检查）。
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from nl2sql.config import get_settings, setup_logging
from nl2sql.db import build_db
from nl2sql.embedding import build_embedder
from nl2sql.graph import Text2SQLGraph
from nl2sql.llm import build_llm
from nl2sql.pipeline import Text2SQLPipeline
from nl2sql.retrieval import build_retriever

from examples.grg_schema import build_registry, build_semantic_layer, build_store

QUESTIONS = [
    ("正常路径", "各业务线的检测准时率是多少"),
    ("空结果 -> Critic 打回", "西南区可靠性试验的准时完成率是多少"),
    ("长尾说法 -> 向量召回兜住", "上个月华东片区可靠性这块的验收及时比例是多少"),
]


def _components(settings):
    registry = build_registry(settings.db.dialect)
    store = build_store()
    llm = build_llm(settings.llm)
    db = build_db(settings.db, registry)
    embedder = build_embedder(settings.embedding, settings.llm)
    retriever = build_retriever(settings, store, embedder)
    return registry, store, llm, db, retriever


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log.level, settings.log.fmt)
    llm_critic = "--llm-critic" in sys.argv

    registry, store, llm, db, retriever = _components(settings)
    kwargs = dict(
        registry=registry, store=store, llm=llm, db=db,
        top_k=settings.retrieval.top_k, min_score=settings.retrieval.min_score,
        max_retry=settings.pipeline.max_retry,
    )
    pipeline = Text2SQLPipeline(**kwargs)
    graph = Text2SQLGraph(**kwargs, retriever=retriever, critique_llm=llm_critic)
    pipeline.retriever = retriever  # 两条编排共用同一检索器（含向量召回）

    print("=" * 74)
    print("图的节点与流转（mermaid）：")
    for line in graph.mermaid().splitlines():
        if any(k in line for k in ("graph TD", "__start__", "-->", "retrieve(", "critique(")):
            print("   ", line.strip())

    for label, question in QUESTIONS:
        print("\n" + "=" * 74)
        print(f"【{label}】{question}")

        res = graph.run(question)
        print("  ── LangGraph 编排 ──")
        for s in res.graph_steps:
            print("     ·", s)
        print(f"     结果：source={res.source.value}  行数={len(res.rows)}  值={res.rows[:3]}")
        print(f"     评审：{res.critique}")

        p_res, cols, rows = pipeline.query(question)
        print("  ── 手写 pipeline ──")
        print(f"     结果：source={p_res.source.value}  行数={len(rows)}  值={rows[:3]}")
        same = (p_res.sql or "").strip() == (res.sql or "").strip()
        print(f"     两者 SQL 一致：{'是' if same else '否（重试次数/评审介入导致，属正常）'}")

    print("\n提示：加 --llm-critic 可额外开启 LLM 结果复核（会多一次模型调用）。")


if __name__ == "__main__":
    main()
