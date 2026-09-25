"""元数据接入层：表结构从哪来、怎么发现它变了。

为什么要有这一层（对应 JD「对接企业数据系统」）：
- Demo 阶段表结构手写在 examples/grg_schema.py —— 这是**演示形态**，不是生产形态；
- 生产里表结构必须从**元数据中心 / 数仓 API** 自动获取，否则：
  ① 上游加个字段，问数系统不知道 → LLM 继续幻觉旧字段；
  ② 改个字段类型，校验层的白名单还是旧的 → 拦错或放错；
- 所以这一层提供三件事：
  1) `load()`：表结构的**单一事实来源**（Static / Api 两种实现，可插拔）；
  2) `fingerprint()`：结构指纹 —— 变更检测的依据（哪怕只加了一列也能发现）；
  3) `diff_tables()`：把「变了什么」讲清楚（加/删表、加/删列、类型变更），
     供上线前 review 与告警使用 —— schema 变更需要被**主动发现**，而不是等答错了再排查。

ApiMetadataProvider 的契约（与常见元数据中心/数仓 catalog API 对齐）：
    GET {base_url}/tables        Authorization: Bearer <token>
    响应: [{"name": ..., "description": ..., "columns": {"col": "type"},
            "foreign_keys": [[col, ref_table, ref_col]], "sample_values": {"col": [...]}}]
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Callable, Optional

from .models import TableSchema

_log = logging.getLogger("nl2sql.metadata")


# ---------------------------------------------------------------------------
# 提供者
# ---------------------------------------------------------------------------

class MetadataProvider(ABC):
    """表结构的单一事实来源。"""

    @abstractmethod
    def load(self) -> list[TableSchema]:
        """拉取全量表结构。实现应当幂等、可重复调用。"""


class StaticMetadataProvider(MetadataProvider):
    """静态提供者：表结构来自代码内的领域知识库（Demo / 离线测试形态）。"""

    def __init__(self, loader: Callable[[], list[TableSchema]]):
        self._loader = loader

    def load(self) -> list[TableSchema]:
        return list(self._loader())


class ApiMetadataProvider(MetadataProvider):
    """从元数据中心 API 拉取表结构（生产形态）。

    - 进程内按 TTL 缓存，避免每次提问都打元数据服务；
    - 网络失败抛出异常由上层处理：**宁可启动失败，也不能静默用空 schema**
      （空 schema 会让 LLM 只能瞎编，比不可用更危险）。
    - `fetch_fn` 供测试注入，生产走 httpx。
    """

    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout: float = 10.0,
        cache_ttl: float = 300.0,
        fetch_fn: Optional[Callable[[str, dict], object]] = None,
    ):
        if not base_url:
            raise ValueError("ApiMetadataProvider 需要 METADATA__BASE_URL")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._fetch_fn = fetch_fn or self._http_get
        self._cache: Optional[list[TableSchema]] = None
        self._cached_at = 0.0

    def load(self) -> list[TableSchema]:
        now = time.monotonic()
        if self._cache is not None and now - self._cached_at < self.cache_ttl:
            return self._cache
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = self._fetch_fn(f"{self.base_url}/tables", headers)
        tables = self._parse(data)
        if not tables:
            raise ValueError(f"元数据中心返回空表结构（{self.base_url}/tables），拒绝使用")
        self._cache, self._cached_at = tables, now
        return tables

    # ---- 可覆写：测试注入 / 换 HTTP 库 ----

    def _http_get(self, url: str, headers: dict):
        import httpx  # 懒加载

        resp = httpx.get(url, headers=headers, timeout=self.timeout, trust_env=False)
        resp.raise_for_status()
        return resp.json()

    def _parse(self, data) -> list[TableSchema]:
        """把 API 的 JSON 转成 TableSchema；字段缺失时给出明确错误而不是静默吞掉。"""
        if isinstance(data, dict):  # 兼容 {"tables": [...]} 包裹
            data = data.get("tables") or []
        out: list[TableSchema] = []
        for i, item in enumerate(data):
            try:
                out.append(
                    TableSchema(
                        name=item["name"],
                        columns=dict(item.get("columns") or {}),
                        description=item.get("description", ""),
                        foreign_keys=[tuple(fk) for fk in item.get("foreign_keys") or []],
                        sample_values=dict(item.get("sample_values") or {}),
                    )
                )
            except (KeyError, TypeError) as e:
                raise ValueError(f"元数据第 {i} 条格式非法（缺少 {e}）: {str(item)[:120]}") from e
        return out


def build_metadata_provider(settings):
    """按配置装配：provider=api 走元数据中心；否则用代码内领域知识库。"""
    md = getattr(settings, "metadata", None)
    provider = getattr(md, "provider", "static") if md else "static"
    if provider == "api":
        return ApiMetadataProvider(
            base_url=md.base_url,
            token=md.token,
            timeout=md.timeout,
            cache_ttl=md.cache_ttl,
        )
    from examples.grg_schema import build_tables  # 懒加载，避免包依赖倒置

    return StaticMetadataProvider(build_tables)


# ---------------------------------------------------------------------------
# 变更检测：指纹 + diff
# ---------------------------------------------------------------------------

def fingerprint(tables: list[TableSchema]) -> str:
    """结构指纹：表名/字段名/字段类型/外键任一变化都会改变指纹。"""
    canonical = [
        {
            "name": t.name,
            "columns": sorted(t.columns.items()),
            "foreign_keys": sorted([list(fk) for fk in t.foreign_keys]),
            "description": t.description,
        }
        for t in sorted(tables, key=lambda x: x.name)
    ]
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def diff_tables(old: list[TableSchema], new: list[TableSchema]) -> dict:
    """结构 diff：加了/删了哪些表、哪些列，哪些列类型变了。"""
    o = {t.name: t for t in old}
    n = {t.name: t for t in new}
    report: dict = {
        "added_tables": sorted(set(n) - set(o)),
        "removed_tables": sorted(set(o) - set(n)),
        "changed_columns": {},
    }
    for name in sorted(set(o) & set(n)):
        oc, nc = o[name].columns, n[name].columns
        added = sorted(set(nc) - set(oc))
        removed = sorted(set(oc) - set(nc))
        type_changed = [
            (c, oc[c], nc[c]) for c in sorted(set(oc) & set(nc)) if oc[c] != nc[c]
        ]
        if added or removed or type_changed:
            report["changed_columns"][name] = {
                "added": added,
                "removed": removed,
                "type_changed": type_changed,
            }
    return report


def format_diff(report: dict) -> str:
    """把 diff 渲染成人能读的变更报告。"""
    lines: list[str] = []
    if report["added_tables"]:
        lines.append(f"新增表: {report['added_tables']}")
    if report["removed_tables"]:
        lines.append(f"删除表: {report['removed_tables']}")
    for table, ch in report["changed_columns"].items():
        if ch["added"]:
            lines.append(f"{table}: 新增列 {ch['added']}")
        if ch["removed"]:
            lines.append(f"{table}: 删除列 {ch['removed']}")
        for col, ot, nt in ch["type_changed"]:
            lines.append(f"{table}.{col}: {ot} -> {nt}")
    return "\n".join(lines) if lines else "无结构变更"
