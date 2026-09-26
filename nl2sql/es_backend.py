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

    def ppl_supported(self) -> bool:
        """探测 /_plugins/_ppl 端点：OpenSearch 为 True，普通 Elasticsearch 为 False。"""
        try:
            r = self._client.get("/_plugins/_ppl/stats", headers=self._headers())
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def execute_ppl(self, ppl_query: str, ir=None) -> tuple[list[str], list[tuple]]:
        """真执行 PPL（OpenSearch _plugins/_ppl）。失败抛 EsError，上层降级。

        PPL 响应契约：{"schema": [{"name": ..., "type": ...}], "datarows": [[...], ...]}

        ⚠️ 列序差异（真机踩坑）：OpenSearch 的 `stats count() as cnt by region` 返回
        `schema = ['cnt', 'region']` —— **聚合列在 by 字段之前**，与 DSL 侧
        `parse_es_response` 的 `[分组, 指标...]` 约定相反。传 ir 进来即可按列名重排对齐契约，
        否则上层按 [key, value] 取值会整体错位（表现为"区域名和数字互换"）。
        """
        try:
            r = self._client.post(
                "/_plugins/_ppl", json={"query": ppl_query}, headers=self._headers())
            if r.status_code != 200:
                detail = r.text[:300]
                # 已知引擎限制：Calcite 用 ISO-8859-1 编码字面量，非 ASCII（中文）直接 500。
                # 这不是查询写错了，换语法（U&'..'/like/match/双引号）都绕不过去，要如实告知。
                if "ISO-8859-1" in detail or "CalciteException" in detail:
                    raise EsError(
                        "PPL 引擎无法处理非 ASCII 字面量：Calcite 按 ISO-8859-1 编码中文字符串失败"
                        "（OpenSearch PPL 的已知限制，非查询语法错误）。"
                        "该问题查询请改用 DSL 档执行，或把过滤字段换成 ASCII 编码字段。"
                    )
                raise EsError(f"PPL {r.status_code}: {detail}")
            body = r.json()
            names = [f.get("name", "") for f in body.get("schema", [])]
            raw_rows = [tuple(row) for row in body.get("datarows", [])]
            return self._reorder_ppl(names, raw_rows, ir)
        except EsError:
            raise
        except Exception as e:  # noqa: BLE001
            raise EsError(f"PPL 请求失败: {e}") from e

    @staticmethod
    def _reorder_ppl(names: list[str], raw_rows: list[tuple], ir) -> tuple[list[str], list[tuple]]:
        """把 PPL 的列序重排成与 DSL 侧一致的 `[分组, 指标...]`。"""
        group_by = getattr(ir, "group_by", None) if ir is not None else None
        metric_names = [m.name for m in getattr(ir, "metrics", [])] if ir is not None else []

        if group_by and group_by in names:
            order = [group_by] + [n for n in metric_names if n in names]
            if len(order) != len(names):          # 有 PPL 多返回的列 -> 追加到尾部，不丢数据
                order += [n for n in names if n not in order]
            idx = [names.index(n) for n in order]
            return order, [tuple(r[i] for i in idx) for r in raw_rows]

        if group_by and len(names) == 2:          # 兜底：列名对不上但确实是"分组+指标"两列 -> 换序
            return [names[1], names[0]], [tuple(reversed(r)) for r in raw_rows]

        if not group_by and metric_names and all(n in names for n in metric_names):
            idx = [names.index(n) for n in metric_names]
            return metric_names, [tuple(r[i] for i in idx) for r in raw_rows]

        return names, raw_rows

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


def resolve_index(settings, mode: str = "es") -> str:
    """按模式解析要查询的索引名（PPL 模式可用 ES__PPL_INDEX 单独指定，留空复用 ES__INDEX）。"""
    es = getattr(settings, "es", None)
    default_index = getattr(es, "index", "") or "device_events"
    if mode == "ppl":
        return getattr(es, "ppl_index", "") or default_index
    return default_index


def build_es_backend(settings, mode: str = "es") -> Optional[ElasticsearchBackend]:
    """按配置装配；未启用/缺配置返回 None（上层优雅降级为纯 SQL）。

    mode="es"  -> 用 ES__HOST（Elasticsearch，**ES DSL 真执行**）
    mode="ppl" -> 优先用 ES__PPL_HOST（OpenSearch，**PPL 真执行**）；未配置则回退 ES__HOST，
                  此时若集群不是 OpenSearch（没有 _plugins/_ppl 端点），PPL 会自动降级为
                  「仅编译」——语法正确性仍由编译器保证，只是不真跑。

    设计意图：把「换引擎」做成配置项而不是分支代码 —— 网页上的 SQL/DSL/PPL 切换，
    底层就是同一个 `ElasticsearchBackend`（REST 直连、只暴露只读方法）指向不同端点。
    """
    es = getattr(settings, "es", None)
    if not es:
        return None
    if mode == "ppl":
        host = getattr(es, "ppl_host", "") or es.host
        user = getattr(es, "ppl_user", "") or es.user
        password = getattr(es, "ppl_password", "") or es.password
        timeout = getattr(es, "ppl_timeout", None) or es.timeout
        enabled = bool(getattr(es, "enabled", False) or getattr(es, "ppl_enabled", False))
    else:
        host, user, password, timeout = es.host, es.user, es.password, es.timeout
        enabled = bool(getattr(es, "enabled", False))
    if not enabled or not host:
        return None
    return ElasticsearchBackend(
        host=host, user=user, password=password, timeout=timeout,
    )

