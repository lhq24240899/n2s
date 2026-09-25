"""Elasticsearch 执行器：REST 直连（复用 httpx，不引入 elasticsearch-py 依赖）。

设计要点：
- **只读**：本类只暴露 search / count / ping，没有任何写方法——与 MCP/API 的只读承诺一致；
  批量写入只出现在 `examples/setup_es_demo.py` 的独立函数里（灌演示数据），不在执行器上。
- 8.x 兼容：显式带 `Content-Type` 与兼容头，避免客户端版本绑架集群版本。
- 异常处理与 PsycopgRunner 同 philosophy：坏连接不留给下一次。
"""
from __future__ import annotations

from typing import Any, Optional

import httpx


class EsError(Exception):
    """ES 查询失败（网络/鉴权/语法）。接口层映射为 503。"""


class ElasticsearchBackend:
    def __init__(
        self,
        host: str,
        user: str = "elastic",
        password: str = "",
        timeout: float = 15.0,
        transport=None,   # 测试注入：httpx.MockTransport 或任意 httpx.Transport
    ):
        if not host:
            raise ValueError("ES__HOST 未配置")
        self.host = host.rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(
            base_url=self.host,
            auth=(user, password) if user else None,
            timeout=timeout,
            transport=transport,
            trust_env=False,   # 与 LLM/embedding 同一策略：不吃本机环境代理
        )

    # ---------------- 基础 ----------------

    def _headers(self) -> dict:
        # 用通用 application/json，而不是 application/vnd.elasticsearch+json; compatible-with=N。
        # 真机踩坑（阿里云 ES 9.3.2）：带 compatible-with=8 **或** =9 都会被判为非法 media type，
        # 直接 400 media_type_header_exception —— 客户端不该绑架集群版本，通用头 7/8/9 通吃。
        return {"Content-Type": "application/json", "Accept": "application/json"}

    def ping(self) -> tuple[bool, str]:
        """健康探测：返回 (ok, 版本或错误)。"""
        try:
            r = self._client.get("/", headers=self._headers())
            r.raise_for_status()
            version = (r.json().get("version") or {}).get("number", "unknown")
            return True, version
        except Exception as e:  # noqa: BLE001
            return False, str(e)[:200]

    def search(self, index: str, body: dict) -> dict:
        """执行 _search。失败抛 EsError（上层决定降级）。"""
        try:
            r = self._client.post(f"/{index}/_search", json=body, headers=self._headers())
            if r.status_code != 200:
                detail = str(r.json().get("error", {}))[:300] if r.headers.get("content-type", "").startswith("application/json") else r.text[:300]
                raise EsError(f"ES {r.status_code}: {detail}")
            return r.json()
        except EsError:
            raise
        except Exception as e:  # noqa: BLE001
            raise EsError(f"ES 请求失败: {e}") from e

    # ---------------- 与 DBRunner 契约对齐的查询入口 ----------------

    def query(self, index: str, body: dict, ir) -> tuple[list[str], list[tuple]]:
        """search + 解析，返回 (列名, 行数据)——和 PsycopgRunner.execute 同一契约。"""
        from .dsl import parse_es_response

        body = dict(body)
        body.setdefault("size", 0)   # 聚合场景不取原始文档
        resp = self.search(index, body)
        return parse_es_response(resp, ir)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


def build_es_backend(settings) -> Optional[ElasticsearchBackend]:
    """按配置装配；未启用/缺配置返回 None（上层优雅降级为纯 SQL）。"""
    es = getattr(settings, "es", None)
    if not es or not getattr(es, "enabled", False) or not es.host:
        return None
    return ElasticsearchBackend(
        host=es.host, user=es.user, password=es.password,
        timeout=es.timeout,
    )
