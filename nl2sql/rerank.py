"""rerank 精排层：混合召回（RRF）之后，对候选再做一次相关性精排。

为什么需要两阶段？
- **召回（recall）**追求"不漏"，宁可多带候选，代价是混入噪声；
- **精排（rerank）**追求"排序准"，只看少量候选、可以用更贵的模型。
这是工业界标准做法，JD 里说的"优化召回精度"通常就指这条链路。

本项目的实现：
- 实测网关 701 个模型里**没有** rerank/bge/jina/cohere 类模型，
  所以默认用 **LLM 打分精排**（listwise：一次调用给所有候选打 0~10 分并返回结构化结果）。
- 抽象成 `Reranker` 接口：将来换成 cross-encoder 或专用 rerank API，只需新增一个类，
  上层（`HybridDocRetriever`）完全不用改。

容错：LLM 返回无法解析时不报错，退回原顺序并在 reasons 里注明降级，保证主流程可用。
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod

from .llm import LLMClient
from .vectorstore import DocHit

RERANK_SYSTEM_PROMPT = (
    "你是一个检索结果相关性评估器。针对用户问题，给每条候选资料打 0~10 的相关性分数，"
    "10 表示直接相关且能回答问题，0 表示完全无关。只输出分数，不要解释。"
)


def _parse_scores(raw: str, n: int) -> dict[int, float]:
    """从 LLM 输出里解析「编号 -> 分数」。

    兼容多种格式：`1: 8`、`1. 8分`、`[1] 8`、以及 JSON 形式的 {"1": 8}。
    """
    scores: dict[int, float] = {}
    for m in re.finditer(r"(\d{1,2})\s*[:：.、\)\]]?\s*(10|\d(?:\.\d+)?)", raw):
        idx, val = int(m.group(1)), float(m.group(2))
        if 1 <= idx <= n and 0 <= val <= 10 and idx not in scores:
            scores[idx] = val
    return scores


class Reranker(ABC):
    """精排接口。"""

    @abstractmethod
    def rerank(self, question: str, docs: list[DocHit], top_k: int) -> list[DocHit]:
        ...


class NoopReranker(Reranker):
    """不精排（关闭 rerank 时使用），保持融合后的原顺序。"""

    def rerank(self, question: str, docs: list[DocHit], top_k: int) -> list[DocHit]:
        return docs[:top_k]


class LLMReranker(Reranker):
    """用 LLM 做 listwise 打分精排（一次调用给所有候选打分）。

    除了重排，还做**低分截断**：精排分数 < `min_score` 的候选直接丢弃。
    这一步很关键——召回阶段为了"不漏"会带进噪声，若原样塞进 prompt，
    反而会干扰 LLM 生成（这也是"降低模型幻觉"的一环）。
    """

    def __init__(self, llm: LLMClient, max_chars: int = 400, min_score: float = 3.0):
        self.llm = llm
        self.max_chars = max_chars
        self.min_score = min_score

    def _prompt(self, question: str, docs: list[DocHit]) -> str:
        parts = [f"# 用户问题\n{question}\n", "# 候选资料"]
        for i, d in enumerate(docs, start=1):
            parts.append(f"[{i}] {d.title}\n{d.content[: self.max_chars]}")
        parts.append(
            "# 输出格式\n每行一条，格式为「编号: 分数」，例如：\n1: 8\n2: 3\n"
            f"共 {len(docs)} 条，必须全部给出。"
        )
        return "\n".join(parts)

    def rerank(self, question: str, docs: list[DocHit], top_k: int) -> list[DocHit]:
        if len(docs) <= 1:
            return docs[:top_k]

        try:
            raw = self.llm.generate(self._prompt(question, docs), RERANK_SYSTEM_PROMPT)
            parsed = _parse_scores(raw or "", len(docs))
        except Exception:  # noqa: BLE001
            parsed = {}

        if not parsed:
            # 降级：保持融合顺序，并在 reasons 里说明，便于排查而不是静默出错
            for d in docs[:top_k]:
                d.reasons = list(d.reasons) + ["LLM 精排：解析失败，保留融合顺序"]
            return docs[:top_k]

        ranked = sorted(
            range(len(docs)),
            key=lambda i: (-parsed.get(i + 1, 0.0), i),
        )
        kept = [i for i in ranked if parsed.get(i + 1, 0.0) >= self.min_score]
        if not kept:  # 全都被截断时至少保留最高分那条，避免答案凭空缺失
            kept = ranked[:1]

        out: list[DocHit] = []
        for rank, i in enumerate(kept[:top_k], start=1):
            d = docs[i]
            score = parsed.get(i + 1, 0.0)
            d.reasons = list(d.reasons) + [f"LLM 精排 {score:.0f}/10（精排 rank{rank}）"]
            out.append(d)

        dropped = len(ranked) - len(kept)
        if dropped > 0:
            for d in out[:1]:
                d.reasons = list(d.reasons) + [f"低分截断：丢弃 {dropped} 条无关候选"]
        return out


def build_reranker(settings, llm: LLMClient | None) -> Reranker:
    """工厂：未开启或没有 LLM 时用 NoopReranker，保证上层无需判空。"""
    if not settings.kb.rerank or llm is None:
        return NoopReranker()
    return LLMReranker(llm, min_score=settings.kb.rerank_min_score)
