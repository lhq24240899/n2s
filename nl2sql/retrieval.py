"""检索层：标签/关键词打分（可解释）⊕ 向量语义召回（可选），RRF 融合。

设计取舍（面试常问）：
- **只上标签**：可解释、零依赖，对「指标/维度/意图」这类强结构化查询效果足够好，
  但长尾问句（用户换了一种说法）召回差。
- **只上向量**：能覆盖同义改写，但黑盒、难调试、冷启动难。
- 所以这里做成**两路 + RRF 融合**：标签分与向量分各自保留独立的名次与原因，
  最终 `reasons` 里能同时看到「标签命中:华东」和「向量相似度 0.83（rank2）」——
  既提召回，又不丢可解释性。

注意：`min_score` 阈值只作用于**纯标签模式**（标签分是绝对分）；
向量模式用 RRF 名次分（1/(k+rank)，绝对值很小），因此改为
「标签分 > 0 或 向量相似度 ≥ example_min_sim」作为准入条件。
"""
from __future__ import annotations

import re
from typing import Optional

from .embedding import Embedder
from .fusion import rrf_fuse
from .knowledge import SQLExampleStore
from .models import RetrievalHit, SQLExample
from .vectorstore import PgExampleVectorIndex

# 意图粗判：用正则把自然语言映射到示例的 intent 标签
_AGG_RE = re.compile(r"(多少|统计|总数|求和|汇总|合计|sum|count|平均|占比)", re.IGNORECASE)
_TOPN_RE = re.compile(r"(前|top|排名|最高|最大|最少|最低|排行)", re.IGNORECASE)


class RetrievalService:
    def __init__(
        self,
        store: SQLExampleStore,
        min_score: float = 1.0,
        *,
        vector_index: Optional[PgExampleVectorIndex] = None,
        embedder: Optional[Embedder] = None,
        rrf_k: int = 60,
        vector_candidates: int = 10,
        vector_min_sim: float = 0.30,
    ):
        self.store = store
        self.min_score = min_score
        self.vector_index = vector_index
        self.embedder = embedder
        self.rrf_k = rrf_k
        self.vector_candidates = vector_candidates
        self.vector_min_sim = vector_min_sim

    # ---------------- 打分 ----------------

    def _score(self, q: str, ex: SQLExample) -> tuple[float, list[str]]:
        score = 0.0
        reasons: list[str] = []

        # 1) 标签命中：domain / intent / metrics / dimensions / keywords
        for tag in ex.domain + ex.intent + ex.metrics + ex.dimensions + ex.keywords:
            if tag and tag in q:
                score += 2.0
                reasons.append(f"标签命中:{tag}")

        # 2) 表名命中
        for t in ex.tables:
            if t in q:
                score += 1.5
                reasons.append(f"表名命中:{t}")

        # 3) 问题文本相似（字符集合重叠，替代向量；仅作微弱加成）
        overlap = len(set(q) & set(ex.question)) / max(len(set(q)), 1)
        if overlap > 0.3:
            score += overlap
            reasons.append(f"文本重叠:{overlap:.2f}")

        # 4) 意图粗判（正则 -> intent 标签）
        if _AGG_RE.search(q) and "聚合" in ex.intent:
            score += 1.5
            reasons.append("意图命中:聚合")
        if _TOPN_RE.search(q) and "TopN" in ex.intent:
            score += 1.5
            reasons.append("意图命中:TopN")

        return score, reasons

    # ---------------- 对外 ----------------

    def retrieve(self, question: str, top_k: int = 5) -> list[RetrievalHit]:
        if self.vector_index is None or self.embedder is None:
            return self._retrieve_by_tags(question, top_k)
        return self._retrieve_hybrid(question, top_k)

    # ---------------- 纯标签（原行为，保持向后兼容） ----------------

    def _retrieve_by_tags(self, question: str, top_k: int) -> list[RetrievalHit]:
        hits: list[RetrievalHit] = []
        for ex in self.store.all():
            s, reasons = self._score(question, ex)
            if s >= self.min_score:
                hits.append(RetrievalHit(ex, s, reasons))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    # ---------------- 两路 + RRF ----------------

    def _retrieve_hybrid(self, question: str, top_k: int) -> list[RetrievalHit]:
        examples = self.store.all()
        scored = {ex.id: self._score(question, ex) for ex in examples}
        by_id = {ex.id: ex for ex in examples}

        # 路 1：标签/关键词（绝对分，取 >0 的按分降序）
        tag_ranked = [
            ex_id
            for ex_id, (s, _) in sorted(scored.items(), key=lambda kv: -kv[1][0])
            if s > 0
        ]

        # 路 2：向量语义召回（余弦相似度降序）
        vec_pairs = self.vector_index.search(
            self.embedder.embed_one(question), top_k=self.vector_candidates
        )
        vec_ranked = [i for i, sim in vec_pairs if sim >= self.vector_min_sim]
        vec_sim = dict(vec_pairs)
        vec_rank = {i: r for r, i in enumerate(vec_ranked, start=1)}

        # RRF 融合
        fused = rrf_fuse([tag_ranked, vec_ranked], k=self.rrf_k)

        hits: list[RetrievalHit] = []
        for ex_id, rrf_score in fused[: max(top_k * 3, top_k)]:
            ex = by_id.get(ex_id)
            if ex is None:
                continue
            s, base_reasons = scored[ex_id]
            reasons = list(base_reasons)
            if s > 0:
                reasons.append(f"标签分 {s:.2f}（rank{tag_ranked.index(ex_id) + 1}）")
            if ex_id in vec_rank:
                reasons.append(
                    f"向量相似度 {vec_sim[ex_id]:.3f}（rank{vec_rank[ex_id]}）"
                )
            reasons.append(f"RRF 融合得分 {rrf_score:.4f}")
            hits.append(RetrievalHit(ex, round(rrf_score, 6), reasons))
            if len(hits) >= top_k:
                break
        return hits


def build_retriever(settings, store: SQLExampleStore, embedder: Optional[Embedder] = None):
    """工厂：给了 embedder + DSN 就启用向量召回，否则退回纯标签检索。"""
    vector_index = None
    if embedder is not None and settings.db.dsn:
        vector_index = PgExampleVectorIndex(
            dsn=settings.db.dsn,
            table=settings.kb.example_table,
            dim=settings.embedding.dim,
            timeout=settings.db.timeout,
        )
    return RetrievalService(
        store,
        min_score=settings.retrieval.min_score,
        vector_index=vector_index,
        embedder=embedder,
        rrf_k=settings.kb.rrf_k,
        vector_candidates=max(settings.kb.example_top_k * 2, 10),
        vector_min_sim=settings.kb.example_min_sim,
    )
