"""检索层：纯标签 + 关键词，零向量依赖，命中原因完全可解释。

设计取舍（面试常问）：
- 为什么不直接上向量？向量召回黑盒、难调试、冷启动难。标签/关键词可解释、零依赖，
  对「指标/维度/意图」这类强结构化查询效果足够好。
- 何时升级？当标签覆盖不到的长尾问题变多时，再叠加 BM25 或向量，但保留 reasons 可解释性。
"""
from __future__ import annotations

import re
from typing import Optional

from .knowledge import SQLExampleStore
from .models import RetrievalHit, SQLExample

# 意图粗判：用正则把自然语言映射到示例的 intent 标签
_AGG_RE = re.compile(r"(多少|统计|总数|求和|汇总|合计|sum|count|平均|占比)", re.IGNORECASE)
_TOPN_RE = re.compile(r"(前|top|排名|最高|最大|最少|最低|排行)", re.IGNORECASE)


class RetrievalService:
    def __init__(self, store: SQLExampleStore, min_score: float = 1.0):
        self.store = store
        self.min_score = min_score

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

    def retrieve(self, question: str, top_k: int = 5) -> list[RetrievalHit]:
        hits: list[RetrievalHit] = []
        for ex in self.store.all():
            s, reasons = self._score(question, ex)
            if s >= self.min_score:
                hits.append(RetrievalHit(ex, s, reasons))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]
