"""多路召回的融合工具（与具体存储/模型无关，可复用）。

**RRF（Reciprocal Rank Fusion）**：`score(d) = Σ 1/(k + rank_i(d))`

只用**名次**、不用分数尺度，所以「标签分 / trgm 相似度 / 向量余弦 / LLM 打分」
这些量纲完全不同的信号可以直接融合，**无需归一化、无需调权重**——鲁棒且好解释。

`k` 取 60 是原论文（Cormack et al., 2009）的默认值：k 越大越"平权"，
越小越放大头部名次的差异。
"""
from __future__ import annotations


def rrf_fuse(ranked_id_lists: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """倒数排名融合：输入多路「按名次排好的 id 列表」，返回 (id, rrf_score) 降序。"""
    scores: dict[str, float] = {}
    for ids in ranked_id_lists:
        for rank, doc_id in enumerate(ids, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
