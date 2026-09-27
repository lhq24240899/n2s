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

缓存为什么放在**模块级**而不是实例级？

- 一次 SQL 提问里，**同一句问题会被 embed 两次**：SQL 示例检索（`RetrievalService`）
  与知识库文档检索（`HybridDocRetriever`）各持一个 Embedder 实例。
- 实例级缓存互不相通，第二次照样要走网络——白付一整次往返。
  提到模块级后，key 用 `(base_url, model, text)`，跨实例直接命中。
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from hashlib import blake2b
from typing import Iterable, Optional

from .config import EmbeddingSettings

# ---------------------------------------------------------------------------
# 进程级共享的向量缓存
# ---------------------------------------------------------------------------
# key = (base_url, model, text)：带上网关与模型名，避免换了模型/网关却命中旧向量。
_EMBED_CACHE: dict[tuple[str, str, str], list[float]] = {}
_EMBED_CACHE_MAX = 512
_EMBED_CACHE_LOCK = threading.Lock()


def _cache_get(key: tuple[str, str, str]) -> Optional[list[float]]:
    """取缓存（返回副本：调用方改动不会污染缓存）。"""
    with _EMBED_CACHE_LOCK:
        hit = _EMBED_CACHE.get(key)
        return list(hit) if hit is not None else None


def _cache_put(key: tuple[str, str, str], vec: list[float]) -> None:
    """写缓存；超出上限按 FIFO 淘汰最早写入的一条（dict 保序）。"""
    with _EMBED_CACHE_LOCK:
        if len(_EMBED_CACHE) >= _EMBED_CACHE_MAX:
            _EMBED_CACHE.pop(next(iter(_EMBED_CACHE), None), None)
        _EMBED_CACHE[key] = list(vec)


def clear_embed_cache() -> None:
    """清空进程级向量缓存（换模型后或测试里重置用）。"""
    with _EMBED_CACHE_LOCK:
        _EMBED_CACHE.clear()


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
    """OpenAI 兼容 /embeddings 客户端（同文本缓存，**跨实例共享**）。"""

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
        # 缓存 key 前缀：带上 base_url 与模型名，保证跨实例共享也不串味
        self._key_prefix = (base_url, settings.model)

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float] | None] = [None] * len(texts)
        todo: list[tuple[int, str, tuple[str, str, str]]] = []
        for i, t in enumerate(texts):
            key = self._key_prefix + (t,)
            cached = _cache_get(key)
            if cached is not None:
                results[i] = cached
            else:
                todo.append((i, t, key))

        if todo:
            resp = self._client.embeddings.create(
                model=self._model, input=[t for _, t, _ in todo]
            )
            for (i, _t, key), item in zip(todo, resp.data):
                vec = [float(x) for x in item.embedding]
                _cache_put(key, vec)
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
