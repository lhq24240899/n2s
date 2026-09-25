"""LLM 抽象层：统一接口 + OpenAI 兼容真实客户端。

生产接入：在 .env 中配置 LLM__API_KEY / LLM__BASE_URL / LLM__MODEL 即可。
未配置 API key 时 `build_llm` 直接抛错（不再回退 Mock），强制暴露配置缺失，
避免「以为在跑真实模型、实际跑的是假数据」这类隐蔽问题。

真实客户端关键点：
- 温度恒为 0（SQL 生成必须确定性）。
- 走 /v1/chat/completions 兼容协议，base_url 可指向 DeepSeek / 通义 / vLLM / Ollama
  或任意 OpenAI 兼容网关（如本项目的 api.ephone.ai）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .config import LLMSettings


class LLMClient(ABC):
    @abstractmethod
    def generate(self, prompt: str, system: str | None = None) -> str:
        """给定 prompt（与可选 system），返回模型原始输出。"""


class OpenAIClient(LLMClient):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        timeout: float = 30.0,
    ):
        from openai import OpenAI  # 懒加载，未安装时报清晰错误

        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    def generate(self, prompt: str, system: str | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        return resp.choices[0].message.content or ""


def build_llm(settings: LLMSettings) -> LLMClient:
    """工厂：配置齐全则返回真实 OpenAI 兼容客户端；缺 key 直接抛错。"""
    if not settings.api_key:
        raise ValueError(
            "未配置 LLM__API_KEY，无法构建真实 LLM 客户端。"
            "请在 .env 中填入 LLM__BASE_URL / LLM__API_KEY / LLM__MODEL。"
        )
    return OpenAIClient(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        timeout=settings.timeout,
    )
