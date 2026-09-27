"""向量化缓存：**跨实例共享**（回归测试）。

背景（真机 bug）：
一次 SQL 提问里，同一句问题会被 embed 两次——
- SQL 示例检索（`RetrievalService._retrieve_hybrid`）
- 知识库文档检索（`HybridDocRetriever.retrieve`）

路由由两个不同的 Embedder 实例各自持有。缓存若只挂在实例上（旧实现是
`self._cache`），第二次照样要走网络，白付一整次 embedding 往返。
本测试锁死"跨实例不重复调用网络"这一行为。
"""
from __future__ import annotations

import pytest

from nl2sql.config import EmbeddingSettings
from nl2sql.embedding import OpenAIEmbedder, clear_embed_cache


class _Item:
    def __init__(self, embedding: list[float]):
        self.embedding = embedding


class _Resp:
    def __init__(self, data: list[_Item]):
        self.data = data


class _FakeEmbeddings:
    """记录调用次数与入参，返回确定性向量（按文本长度区分，便于断言）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.inputs: list[list[str]] = []

    def create(self, model: str, input: list[str]) -> _Resp:  # noqa: A002 - 对齐 SDK 签名
        self.calls += 1
        self.inputs.append(list(input))
        return _Resp(
            [_Item([float(len(t)), float(sum(map(ord, t)) % 997), 2.0]) for t in input]
        )


class _FakeClient:
    def __init__(self) -> None:
        self.embeddings = _FakeEmbeddings()

    @property
    def calls(self) -> int:
        return self.embeddings.calls

    @property
    def inputs(self) -> list[list[str]]:
        return self.embeddings.inputs


def _make_embedder(model: str = "m1", base_url: str = "http://gw/v1"):
    """构造一个真实 OpenAIEmbedder，但把底层 client 换成假客户端（不联网）。"""
    settings = EmbeddingSettings(model=model, dim=3, base_url=base_url, api_key="test-key")
    emb = OpenAIEmbedder(settings)
    fake = _FakeClient()
    emb._client = fake  # type: ignore[assignment]
    return emb, fake


@pytest.fixture(autouse=True)
def _isolate_cache():
    """缓存是进程级共享的，测试之间必须清干净。"""
    clear_embed_cache()
    yield
    clear_embed_cache()


def test_same_text_embeds_once_across_instances():
    """核心回归：两个实例对同一文本各调一次，网络只被调用一次。"""
    a, fake_a = _make_embedder()
    b, fake_b = _make_embedder()

    text = "华东区上个月可靠性试验的准时完成率是多少"
    va = a.embed_one(text)
    vb = b.embed_one(text)

    assert fake_a.calls + fake_b.calls == 1, "同一句问题被重复向量化了（跨实例缓存失效）"
    assert va == vb, "跨实例应当命中同一份向量"


def test_cache_is_shared_not_per_instance():
    """第一次调用落在哪个实例，另一个实例都应直接命中。"""
    a, fake_a = _make_embedder()
    a.embed_one("问题")

    b, fake_b = _make_embedder()
    b.embed_one("问题")

    assert fake_a.calls == 1
    assert fake_b.calls == 0, "新实例没有命中已有的共享缓存"


def test_different_texts_are_not_conflated():
    a, fake = _make_embedder()
    v1 = a.embed_one("文本一")
    v2 = a.embed_one("文本二")

    assert fake.calls == 2
    assert v1 != v2


def test_different_models_do_not_collide():
    """缓存 key 带模型名：换模型不能命中旧向量。"""
    a, fake_a = _make_embedder(model="m1")
    b, fake_b = _make_embedder(model="m2")

    a.embed_one("同一句")
    b.embed_one("同一句")

    assert fake_a.calls == 1 and fake_b.calls == 1, "不同模型之间串味了"


def test_different_base_urls_do_not_collide():
    """缓存 key 带网关地址：换网关不能命中旧向量。"""
    a, fake_a = _make_embedder(base_url="http://gw-a/v1")
    b, fake_b = _make_embedder(base_url="http://gw-b/v1")

    a.embed_one("同一句")
    b.embed_one("同一句")

    assert fake_a.calls == 1 and fake_b.calls == 1


def test_returned_vector_is_a_copy():
    """返回值是副本：调用方改动不会污染缓存。"""
    a, fake_a = _make_embedder()
    v1 = a.embed_one("别改我")
    v1[0] = 999.0

    b, fake_b = _make_embedder()
    v2 = b.embed_one("别改我")

    assert fake_b.calls == 0
    assert v2[0] != 999.0, "缓存被调用方改坏了"


def test_clear_embed_cache_forces_refetch():
    a, fake_a = _make_embedder()
    a.embed_one("问题")
    assert fake_a.calls == 1

    clear_embed_cache()
    a.embed_one("问题")
    assert fake_a.calls == 2, "清空缓存后应当重新请求"


def test_batch_embed_only_requests_missing_texts():
    """批量接口里已缓存的文本不再出现在请求入参中。"""
    a, fake_a = _make_embedder()
    a.embed_one("已缓存")

    b, fake_b = _make_embedder()
    out = b.embed(["已缓存", "新文本"])

    assert len(out) == 2
    assert fake_b.calls == 1
    assert fake_b.inputs[0] == ["新文本"], "已缓存的文本不应再次请求"
