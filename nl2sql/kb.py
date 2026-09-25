"""企业知识库的混合检索：三路召回 → RRF 融合 → 可解释输出。

为什么是「混合」而不是纯向量？
- 纯向量是黑盒：召回错了很难解释，冷启动也难调。
- 三路信号各管一段，融合后既提召回又保留可解释性：
  | 信号 | 擅长的场景 | 可解释性 |
  |---|---|---|
  | 关键词命中 | 术语、指标名原样出现 | 最高（直接说命中了哪个词） |
  | pg_trgm | 错别字、口语变体、语序不同 | 高（给相似度） |
  | pgvector | 同义改写（"准时率"↔"按期交付比例"） | 中（给相似度） |

**RRF（Reciprocal Rank Fusion）**：`score(d) = Σ 1/(k + rank_i(d))`
只用到**名次**、不用分数尺度，所以三路信号量纲不同也无需调权重——鲁棒且好讲。
"""
from __future__ import annotations

from .embedding import Embedder
from .llm import LLMClient
from .vectorstore import DocHit, PgVectorStore

# 中文停用字符：含这些字的 n-gram 不作为查询词（避免"的是""多少"这类噪声词）
_STOP_CHARS = set("的是了在和有我你他她它们这那个吗呢么多少点把被给对从以及而且但也就是")


def tokenize_cn(text: str, min_n: int = 2, max_n: int = 4, limit: int = 24) -> list[str]:
    """极简中文分词：滑窗 n-gram，过滤含停用字符的片段。

    不引 jieba 是为了零依赖；对"指标名/术语"这类查询词足够用
    （"华东区上个月可靠性试验的准时完成率" -> 命中"准时率""可靠性"等）。
    """
    clean = "".join(ch if ch.isalnum() else " " for ch in text)
    tokens: list[str] = []
    for chunk in clean.split():
        for n in range(min_n, max_n + 1):
            for i in range(len(chunk) - n + 1):
                gram = chunk[i : i + n]
                if any(c in _STOP_CHARS for c in gram):
                    continue
                tokens.append(gram)
    # 去重保序，截断
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:limit]


def rrf_fuse(ranked_id_lists: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """倒数排名融合：多路召回按名次融合，返回 (id, rrf_score) 降序。

    只依赖名次，因此各路分数尺度不同也无需归一化/调权重。
    """
    scores: dict[str, float] = {}
    for ids in ranked_id_lists:
        for rank, doc_id in enumerate(ids, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


class HybridDocRetriever:
    """三路召回 + RRF 融合的文档检索器。"""

    def __init__(
        self,
        store: PgVectorStore,
        embedder: Embedder,
        top_k: int = 4,
        rrf_k: int = 60,
        per_channel: int = 10,
    ):
        self.store = store
        self.embedder = embedder
        self.top_k = top_k
        self.rrf_k = rrf_k
        self.per_channel = per_channel

    def retrieve(self, question: str) -> list[DocHit]:
        tokens = tokenize_cn(question)
        kw = self.store.keyword_search(tokens, top_k=self.per_channel)
        tr = self.store.trgm_search(question, top_k=self.per_channel)
        vec = self.store.vector_search(
            self.embedder.embed_one(question), top_k=self.per_channel
        )

        by_id: dict[str, DocHit] = {}
        for hits in (kw, tr, vec):
            for h in hits:
                if h.id not in by_id:
                    by_id[h.id] = h

        fused = rrf_fuse(
            [[h.id for h in kw], [h.id for h in tr], [h.id for h in vec]], k=self.rrf_k
        )

        out: list[DocHit] = []
        for doc_id, score in fused[: self.top_k]:
            base = by_id[doc_id]
            # 把三路各自的原因都带上，最终给用户看到的是"为什么召回这一条"
            reasons: list[str] = []
            for hits, label in ((kw, "关键词"), (tr, "trgm"), (vec, "向量")):
                rank = next((i + 1 for i, h in enumerate(hits) if h.id == doc_id), None)
                if rank:
                    reasons.append(f"{label} rank{rank}")
            out.append(
                DocHit(
                    id=base.id,
                    title=base.title,
                    content=base.content,
                    source=base.source,
                    score=round(score, 6),
                    reasons=[f"RRF 融合得分 {score:.4f}（" + "、".join(reasons) + "）"]
                    if reasons
                    else [f"RRF 融合得分 {score:.4f}"],
                )
            )
        return out

    def search_summary(self, question: str) -> dict:
        """给 UI 用的检索明细（三路各自的原始结果 + 融合结果），便于解释与调参。"""
        tokens = tokenize_cn(question)
        kw = self.store.keyword_search(tokens, top_k=self.per_channel)
        tr = self.store.trgm_search(question, top_k=self.per_channel)
        vec = self.store.vector_search(
            self.embedder.embed_one(question), top_k=self.per_channel
        )
        return {
            "tokens": tokens,
            "keyword": [(h.id, h.title, h.score) for h in kw],
            "trgm": [(h.id, h.title, round(h.score, 4)) for h in tr],
            "vector": [(h.id, h.title, round(h.score, 4)) for h in vec],
        }


RAG_SYSTEM_PROMPT = (
    "你是计量检测行业的知识助手。只能依据提供的资料回答，"
    "禁止编造资料里没有的数据、数字或结论。"
    "回答要简洁，并在引用到的句子后用 [编号] 标注来源，例如 [1][3]。"
    "若资料不足以回答，就直接说明「资料中未涉及」。"
)


def build_doc_prompt(question: str, docs: list[DocHit], max_chars: int = 1200) -> str:
    """把检索到的文档拼成给 LLM 的资料块（带编号，便于引用）。"""
    parts = [f"# 问题\n{question}\n", "# 资料（只能依据以下内容回答）"]
    for i, d in enumerate(docs, start=1):
        body = d.content[:max_chars]
        src = f"（来源：{d.source}）" if d.source else ""
        parts.append(f"[{i}] {d.title}{src}\n{body}")
    parts.append("# 输出\n用中文作答，引用处标注 [编号]。")
    return "\n".join(parts)


def build_sql_doc_block(docs: list[DocHit], max_chars: int = 1200) -> str:
    """把知识库摘录拼成**注入 SQL 生成 prompt** 的上下文块。

    与 `build_doc_prompt` 的区别：这里不是让 LLM 直接回答，而是给它补充
    「表结构里看不出来的业务口径与已知坑」（如区域不带"区"字、业务线用英文 code）。
    """
    if not docs:
        return ""
    parts = ["# 企业知识库摘录（业务口径参考，生成 SQL 时必须遵守）"]
    for i, d in enumerate(docs, start=1):
        parts.append(f"[{i}] {d.title}\n{d.content[:max_chars]}")
    return "\n".join(parts)


def answer_with_docs(
    llm: LLMClient, question: str, docs: list[DocHit], max_chars: int = 1200
) -> str:
    """基于检索到的文档生成"带引用"的答案（降幻觉：只依据资料作答）。"""
    if not docs:
        return ""
    return llm.generate(build_doc_prompt(question, docs, max_chars), RAG_SYSTEM_PROMPT)


def build_doc_retriever(settings, embedder: Embedder) -> HybridDocRetriever:
    """工厂：从 Settings 构建知识库检索器。"""
    store = PgVectorStore(
        dsn=settings.db.dsn,
        table=settings.kb.table,
        dim=settings.embedding.dim,
        timeout=settings.db.timeout,
    )
    return HybridDocRetriever(
        store=store,
        embedder=embedder,
        top_k=settings.kb.top_k,
        rrf_k=settings.kb.rrf_k,
    )
