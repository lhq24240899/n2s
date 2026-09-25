"""Schema Linking：从「问题 + 检索命中 + 业务知识图谱」中抽取候选表，控制喂给 LLM 的 schema 规模。

为什么需要它：
- 真实数仓有几百张表，全量塞进 prompt 既超 token 又诱导幻觉。
- 这里用四步给出最小相关 schema：
  1) 问题字面匹配表名 / 表的中文描述；
  2) 继承检索命中示例所涉及的表；
  3) **业务知识图谱**：用「业务术语 -> 数据表」的对应关系补候选表；
  4) 兜底：什么都没抽到时给一个最小核心表集合。

第 3 步为什么必要（实测）：
  我们的物理表名是英文（labs / equipment / reports），而用户说的是中文（"实验室""设备"）。
  仅靠字面匹配，`各实验室的设备利用率` 这类问题**一张表都抽不到**，只能靠检索命中兜着走。
  知识图谱补上了这层「业务术语 ↔ 物理表」的映射，让 Schema Linking 真正与业务语言对齐。
"""
from __future__ import annotations

import logging
from typing import Optional

from .knowledge import SchemaRegistry
from .models import RetrievalHit

_log = logging.getLogger("nl2sql.linker")

# 知识图谱中表示「概念 -> 物理表」的关系名
TABLE_REL = "对应表"


class SchemaLinker:
    def __init__(
        self,
        registry: SchemaRegistry,
        fallback_tables: Optional[list[str]] = None,
        graph=None,
    ):
        self.registry = registry
        self.fallback_tables = fallback_tables or []
        # 业务知识图谱（SemanticLayer.graph）。不传则退化为原来的三步逻辑。
        self.graph = graph
        # 本次 linking 的可解释依据（写回 pipeline 日志 / trace，便于排错）
        self.last_reasons: list[str] = []

    def infer_tables(self, question: str, hits: list[RetrievalHit]) -> list[str]:
        names: set[str] = set()
        reasons: list[str] = []

        # 1) 问题中出现表名 / 表的中文描述 -> 直接命中
        for tname, t in self.registry.tables.items():
            if tname in question:
                names.add(tname)
                reasons.append(f"字面命中表名: {tname}")
            if t.description and t.description in question:
                names.add(tname)
                reasons.append(f"字面命中表描述: {t.description} -> {tname}")

        # 2) 继承检索命中的示例所涉及的表
        for h in hits:
            for t in h.example.tables:
                if t in self.registry.tables:
                    names.add(t)
                    reasons.append(f"检索命中继承: {h.example.id} -> {t}")

        # 3) 业务知识图谱：业务术语 -> 物理表
        graph_hits = self._graph_tables(question)
        for table, why in graph_hits:
            names.add(table)
            reasons.append(why)

        # 4) 兜底：什么都没抽到时，给一个最小核心表集合
        #    （生产可改为「要求用户澄清」，避免盲猜）
        if not names and self.fallback_tables:
            names = set(self.fallback_tables)
            reasons.append(f"兜底核心表: {sorted(names)}")

        self.last_reasons = reasons
        # 必须**稳定排序**后返回：set 的迭代顺序受 PYTHONHASHSEED 影响（跨进程随机），
        # 直接 list(set) 会让每次运行喂给 LLM 的 schema 顺序不同 -> 同一问题两次结果不一致。
        # 实测表现：评估集出现 flaky（B02 同输入一次 0.88 一次 0.81）。问数要可复现。
        return sorted(names)

    def _graph_tables(self, question: str) -> list[tuple[str, str]]:
        """用知识图谱把问题里的业务术语映射到物理表。

        只看「对应表」关系（概念 -> 表），不做多跳推理——问数场景里一跳足够，
        多跳容易把无关表拉进 prompt，反而稀释注意力。
        """
        if self.graph is None:
            return []
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for edge in getattr(self.graph, "edges", []):
            if getattr(edge, "rel", "") != TABLE_REL:
                continue
            if not edge.src or edge.src not in question:
                continue
            if edge.dst not in self.registry.tables or edge.dst in seen:
                continue
            seen.add(edge.dst)
            out.append((edge.dst, f"知识图谱: {edge.src} -> {edge.dst}"))
        return out

