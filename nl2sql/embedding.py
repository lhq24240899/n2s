"""向量化层：把文本转成 embedding。

为什么单独抽一层？
- 向量库、混合检索、知识库构建都要"把文本转向量"，抽出来便于换模型 / 加缓存 / 离线测试。
- 默认**复用 LLM 的 base_url / api_key**（本项目用同一个 OpenAI 兼容网关），
  也可用 `EMBEDDING__BASE_URL` / `EMBEDDING__API_KEY` 单独覆盖。

两个工程要点（面试可讲）：
1. **问题侧每次提问都要 embed**（一次网络往返 ~1s），所以内置**同文本缓存**，
   同一句话不重复调用；文档侧只在建库时 embed 一次。
2. `HashingEmbedder` 是确定性的本地实现（hashing trick），不联网、不花钱，
   用于离线单测与"无网演示"——让检索/融合逻辑可以在没有 API key 时被验证。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from hashlib import blake2b
from typing import Iterable

from .config import EmbeddingSettings


class Embedder(ABC):
    """向量化接口。"""

    @property
    @abstractmethod
    def dim(self) -> int:
        """向量维度。"""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化，返回与输入等长的向量列表。"""

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class OpenAIEmbedder(Embedder):
    """OpenAI 兼容 /embeddings 客户端（带同文本缓存）。"""

    def __init__(
        self,
        settings: EmbeddingSettings,
        llm_base_url: str = "",
        llm_api_key: str = "",
        llm_disable_proxy: bool = False,
    ):
        from openai import OpenAI  # 懒加载

        base_url = settings.base_url or llm_base_url
        api_key = settings.api_key or llm_api_key
        if not api_key:
            raise ValueError(
                "未配置 embedding 凭证：请在 .env 填 EMBEDDING__API_KEY 或 LLM__API_KEY"
            )

        http_client = None
        # 默认继承 LLM 的代理策略：同一个网关、同一条网络路径，
        # LLM 若需要绕开本机异常代理，embedding 也需要。
        if settings.disable_proxy or llm_disable_proxy:
            import httpx

            http_client = httpx.Client(trust_env=False)

        self._client = OpenAI(
            base_url=base_url, api_key=api_key, timeout=settings.timeout, http_client=http_client
        )
        self._model = settings.model
        self._dim = settings.dim
        self._cache: dict[str, list[float]] = {}

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float] | None] = [None] * len(texts)
        todo: list[tuple[int, str]] = []
        for i, t in enumerate(texts):
            cached = self._cache.get(t)
            if cached is not None:
                results[i] = cached
            else:
                todo.append((i, t))

        if todo:
            resp = self._client.embeddings.create(
                model=self._model, input=[t for _, t in todo]
            )
            for (i, t), item in zip(todo, resp.data):
                vec = [float(x) for x in item.embedding]
                self._cache[t] = vec
                results[i] = vec

        return [r if r is not None else [] for r in results]


class HashingEmbedder(Embedder):
    """确定性本地向量化（hashing trick + 字符 n-gram）。

    不联网、不花钱、结果可复现；效果不如真模型，但足以驱动与验证
    「检索 → 融合 → 排序」这条链路，也让单测可以完全离线。
    """

    def __init__(self, dim: int = 256, ngram: int = 2):
        self._dim = dim
        self._ngram = ngram

    @property
    def dim(self) -> int:
        return self._dim

    def _grams(self, text: str) -> Iterable[str]:
        t = "".join(ch for ch in text if not ch.isspace())
        n = self._ngram
        for i in range(max(len(t) - n + 1, 1)):
            yield t[i : i + n]

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self._dim
            for g in self._grams(text):
                h = blake2b(g.encode("utf-8"), digest_size=8).digest()
                idx = int.from_bytes(h[:4], "big") % self._dim
                sign = 1.0 if h[4] & 1 else -1.0
                vec[idx] += sign
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / norm for v in vec])
        return out


def build_embedder(settings, llm_settings=None) -> Embedder:
    """工厂：真实 OpenAI 兼容 embedder；未配凭证时退回 HashingEmbedder（保证链路可跑）。"""
    llm_base_url = getattr(llm_settings, "base_url", "") if llm_settings else ""
    llm_api_key = getattr(llm_settings, "api_key", "") if llm_settings else ""
    llm_disable_proxy = bool(getattr(llm_settings, "disable_proxy", False)) if llm_settings else False
    api_key = settings.api_key or llm_api_key
    if not api_key:
        return HashingEmbedder()
    return OpenAIEmbedder(
        settings,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_disable_proxy=llm_disable_proxy,
    )
