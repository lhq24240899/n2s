"""构建企业知识库：建表 + 向量化 + 写入（幂等），并可做检索自检。

运行：
    python examples/setup_kb.py                     # 建库 / 重建（幂等 upsert）
    python examples/setup_kb.py --query "华东区上个月可靠性试验的准时完成率是多少"
    python examples/setup_kb.py --demo              # 内置几个问题跑一遍混合检索

前置：`.env` 里配好 DB__DSN；embedding 默认复用 LLM__BASE_URL / LLM__API_KEY。
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from nl2sql.config import get_settings
from nl2sql.embedding import build_embedder
from nl2sql.kb import build_doc_retriever, tokenize_cn
from nl2sql.llm import build_llm
from nl2sql.vectorstore import PgExampleVectorIndex, PgVectorStore

from examples.kb_docs import DOCS

DEMO_QUESTIONS = [
    "华东区上个月可靠性试验的准时完成率是多少",
    "各业务线的检测准时率是多少",
    "为什么问华南区查不到数据",
    "EMC 是什么意思",
    "设备利用率的口径是什么",
    "ISO/IEC 17025 和 GB/T 27025 有什么区别",
]


def build(settings) -> PgVectorStore:
    embedder = build_embedder(settings.embedding, settings.llm)
    store = PgVectorStore(
        dsn=settings.db.dsn,
        table=settings.kb.table,
        dim=settings.embedding.dim,
        timeout=settings.db.timeout,
    )
    store.ensure_schema()
    print(f"表 {settings.kb.table} 已就绪（pgvector dim={settings.embedding.dim}）")

    vectors = embedder.embed([f"{d['title']}\n{d['content']}" for d in DOCS])
    rows = [
        {**d, "embedding": vec} for d, vec in zip(DOCS, vectors)
    ]
    n = store.upsert(rows)
    print(f"已写入/更新 {n} 篇文档；当前库内共 {store.count()} 篇")
    return store


def build_example_index(settings, embedder) -> int:
    """把 SQL 示例库的「示例问题」向量化，写入示例向量表。

    检索层据此做语义召回（长尾问句换了一种说法也能命中相近范例），
    再与标签分做 RRF 融合。
    """
    from examples.grg_schema import build_store

    idx = PgExampleVectorIndex(
        dsn=settings.db.dsn,
        table=settings.kb.example_table,
        dim=settings.embedding.dim,
        timeout=settings.db.timeout,
    )
    idx.ensure_schema()
    examples = build_store().all()
    vecs = embedder.embed([ex.question for ex in examples])
    n = idx.upsert(
        [
            {"id": ex.id, "question": ex.question, "embedding": v}
            for ex, v in zip(examples, vecs)
        ]
    )
    print(f"示例向量索引：已写入/更新 {n} 条；库内共 {idx.count()} 条")
    idx.close()
    return n


def show_query(settings, question: str) -> None:
    embedder = build_embedder(settings.embedding, settings.llm)
    retriever = build_doc_retriever(settings, embedder, build_llm(settings.llm))
    print("=" * 72)
    print("问题:", question)
    summary = retriever.search_summary(question)
    print("  分词:", summary["tokens"][:12])
    print("  关键词路:", [(t, round(s, 2)) for _, t, s in summary["keyword"]][:4])
    print("  trgm 路 :", [(t, s) for _, t, s in summary["trgm"]][:4])
    print("  向量路  :", [(t, s) for _, t, s in summary["vector"]][:4])
    hits = retriever.retrieve(question)
    print("  ▶ RRF 融合 + 精排 后 Top-%d:" % len(hits))
    for i, h in enumerate(hits, 1):
        print(f"    {i}. {h.title}")
        for r in h.reasons:
            print(f"       · {r}")


def main() -> None:
    settings = get_settings()

    args = sys.argv[1:]
    if "--query" in args:
        q = args[args.index("--query") + 1]
        show_query(settings, q)
        return

    store = build(settings)
    store.close()
    build_example_index(settings, build_embedder(settings.embedding, settings.llm))

    if "--demo" in args:
        for q in DEMO_QUESTIONS:
            show_query(settings, q)
    else:
        print("\n提示：加 --demo 可跑一组检索自检；加 --query \"...\" 可单查一条。")


if __name__ == "__main__":
    main()
