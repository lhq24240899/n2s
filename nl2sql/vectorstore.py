"""企业知识库存储：Neon / PostgreSQL + pgvector + pg_trgm。

为什么不另引一套向量库？
- 结构化数据（表/指标）与文档（口径、标准、术语）都在同一个 Neon 库里，
  天然支持「**向量检索 ⊕ 结构化查询**的混合检索」，不用维护两套数据一致性；
- Neon 免运维，Streamlit Cloud 上零额外服务。

本模块在**同一个表**上提供三路召回信号（供上层做 RRF 融合）：
1. `keyword_search`：精确子串命中数 —— 最可解释（能直接告诉用户"命中了哪个词"）
2. `trgm_search`   ：pg_trgm 相似度 —— 容错（错别字、口语变体、语序不同）
3. `vector_search` ：pgvector 余弦相似度 —— 语义改写（"准时率" ↔ "按期交付比例"）

连接层沿用 BUG-05 的教训：**autocommit + 异常即弃连接**，避免一条坏语句
把整条连接毒化成 `current transaction is aborted`。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DocHit:
    """一条命中的知识库文档。"""

    id: str
    title: str
    content: str
    source: str = ""
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)  # 可解释：这一条因为什么被召回


def _vec_literal(vec: list[float]) -> str:
    """把向量转成 pgvector 的字面量（'[0.1,0.2,...]'）。"""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


class _PgConnMixin:
    """共用的 Postgres 连接管理：autocommit + 异常即弃连接（BUG-05 的教训）。"""

    dsn: str
    timeout: float
    _conn = None

    def _connect(self):
        if self._conn is None or getattr(self._conn, "closed", False):
            import psycopg

            self._conn = psycopg.connect(
                self.dsn, connect_timeout=int(self.timeout), autocommit=True
            )
        return self._conn

    def _discard(self) -> None:
        """丢弃当前连接，下一次调用自动重连（不把坏状态的连接留给后续查询）。"""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _exec(self, sql: str, params: tuple = (), fetch: bool = False):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if fetch:
                    return cur.fetchall()
                return None
        except Exception:  # noqa: BLE001
            self._discard()
            raise

    def close(self) -> None:
        self._discard()


class PgVectorStore(_PgConnMixin):
    """pgvector 文档库：建表 + 写入 + 三路检索。"""

    def __init__(
        self,
        dsn: str,
        table: str = "kb_docs",
        dim: int = 1536,
        timeout: float = 10.0,
    ):
        if not dsn:
            raise ValueError("未配置 DB__DSN，无法使用 pgvector 知识库")
        self.dsn = dsn
        self.table = table
        self.dim = dim
        self.timeout = timeout
        self._conn = None

    # ---------------- 建表 ----------------

    def ensure_schema(self) -> None:
        """幂等建表：扩展 + 主表 + HNSW 向量索引 + trgm 索引。"""
        self._exec("CREATE EXTENSION IF NOT EXISTS vector")
        self._exec("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        self._exec(
            f"""
            CREATE TABLE IF NOT EXISTS {self.table} (
                id        text PRIMARY KEY,
                title     text NOT NULL,
                content   text NOT NULL,
                source    text DEFAULT '',
                tags      jsonb DEFAULT '[]'::jsonb,
                embedding vector({self.dim}),
                updated_at timestamptz DEFAULT now()
            )
            """
        )
        # HNSW：向量近似最近邻索引（余弦距离）
        self._exec(
            f"CREATE INDEX IF NOT EXISTS {self.table}_emb_hnsw "
            f"ON {self.table} USING hnsw (embedding vector_cosine_ops)"
        )
        # trgm：模糊匹配索引
        self._exec(
            f"CREATE INDEX IF NOT EXISTS {self.table}_trgm "
            f"ON {self.table} USING gin ((title || ' ' || content) gin_trgm_ops)"
        )

    # ---------------- 写入 ----------------

    def upsert(self, docs: list[dict]) -> int:
        """写入/更新文档。每个 dict 需含 id/title/content/embedding，可选 source/tags。"""
        if not docs:
            return 0
        rows = [
            (
                d["id"],
                d["title"],
                d["content"],
                d.get("source", ""),
                json.dumps(d.get("tags", []), ensure_ascii=False),  # jsonb 需 JSON 文本
                _vec_literal(d["embedding"]) if d.get("embedding") else None,
            )
            for d in docs
        ]
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    f"""
                    INSERT INTO {self.table} (id, title, content, source, tags, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s::vector)
                    ON CONFLICT (id) DO UPDATE SET
                        title = EXCLUDED.title,
                        content = EXCLUDED.content,
                        source = EXCLUDED.source,
                        tags = EXCLUDED.tags,
                        embedding = EXCLUDED.embedding,
                        updated_at = now()
                    """,
                    rows,
                )
            return len(rows)
        except Exception:  # noqa: BLE001
            self._discard()
            raise

    def count(self) -> int:
        rows = self._exec(f"SELECT COUNT(*) FROM {self.table}", fetch=True)
        return int(rows[0][0]) if rows else 0

    # ---------------- 三路检索 ----------------

    @staticmethod
    def _to_hits(rows, reason: str) -> list[DocHit]:
        hits: list[DocHit] = []
        for r in rows:
            hits.append(
                DocHit(
                    id=r[0],
                    title=r[1],
                    content=r[2],
                    source=r[3] or "",
                    score=float(r[4] or 0.0),
                    reasons=[f"{reason}: {float(r[4] or 0.0):.3f}"],
                )
            )
        return hits

    def vector_search(self, embedding: list[float], top_k: int = 10) -> list[DocHit]:
        """语义召回：按余弦相似度（1 - 余弦距离）排序。"""
        lit = _vec_literal(embedding)
        rows = self._exec(
            f"""
            SELECT id, title, content, source,
                   1 - (embedding <=> %s::vector) AS score
            FROM {self.table}
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (lit, lit, top_k),
            fetch=True,
        )
        return self._to_hits(rows or [], "向量相似度")

    def trgm_search(self, query: str, top_k: int = 10, min_sim: float = 0.10) -> list[DocHit]:
        """模糊召回：pg_trgm `word_similarity`（短查询 vs 长文档的正确选择）。

        注意别用 `similarity()`：那是「整串 vs 整串」的相似度，短查询对长文档
        几乎必然低于默认阈值 0.3，会一路召回为空（实测踩过）。
        `word_similarity(query, doc)` 取「查询三元组 与 文档任意连续片段」的最大相似度，
        正是「短查询命中长文档」需要的语义；生产环境可加 GiST(`gist_trgm_ops`) 索引
        配合 `<%` 操作符走索引加速。
        """
        rows = self._exec(
            f"""
            SELECT id, title, content, source,
                   word_similarity(%s, title || ' ' || content) AS score
            FROM {self.table}
            WHERE word_similarity(%s, title || ' ' || content) >= %s
            ORDER BY score DESC
            LIMIT %s
            """,
            (query, query, min_sim, top_k),
            fetch=True,
        )
        return self._to_hits(rows or [], "trgm 相似度")

    def keyword_search(self, tokens: list[str], top_k: int = 10) -> list[DocHit]:
        """关键词召回：统计查询词在文档中出现的个数（最可解释）。"""
        if not tokens:
            return []
        rows = self._exec(
            f"""
            SELECT id, title, content, source, hits AS score FROM (
                SELECT id, title, content, source,
                       (SELECT COUNT(*) FROM unnest(%s::text[]) AS tk
                         WHERE title || ' ' || content ILIKE '%%' || tk || '%%') AS hits
                FROM {self.table}
            ) t
            WHERE hits > 0
            ORDER BY hits DESC
            LIMIT %s
            """,
            (list(tokens), top_k),
            fetch=True,
        )
        hits = self._to_hits(rows or [], "关键词命中")
        for h in hits:
            h.reasons = [f"关键词命中 {int(h.score)} 个"]
        return hits


class PgExampleVectorIndex(_PgConnMixin):
    """SQL 示例库的向量索引：把「示例问题」向量化，供检索层做**语义召回**。

    为什么单独一张表而不是复用 kb_docs？
    - 语义不同：kb_docs 是"知识文档"，这里是"(问题, SQL) 范例"，落库只需问题文本；
    - 检索层只需 `(example_id, 相似度)`，融合由 `RetrievalService` 用 RRF 完成，
      这样标签分与向量分始终是**两条独立信号**，可解释性不丢。
    """

    def __init__(
        self,
        dsn: str,
        table: str = "sql_example_vec",
        dim: int = 1536,
        timeout: float = 10.0,
    ):
        if not dsn:
            raise ValueError("未配置 DB__DSN，无法使用示例库向量索引")
        self.dsn = dsn
        self.table = table
        self.dim = dim
        self.timeout = timeout
        self._conn = None

    def ensure_schema(self) -> None:
        self._exec("CREATE EXTENSION IF NOT EXISTS vector")
        self._exec(
            f"""
            CREATE TABLE IF NOT EXISTS {self.table} (
                id         text PRIMARY KEY,
                question   text NOT NULL,
                embedding  vector({self.dim}),
                updated_at timestamptz DEFAULT now()
            )
            """
        )
        self._exec(
            f"CREATE INDEX IF NOT EXISTS {self.table}_emb_hnsw "
            f"ON {self.table} USING hnsw (embedding vector_cosine_ops)"
        )

    def upsert(self, rows: list[dict]) -> int:
        """rows: [{'id':..., 'question':..., 'embedding':[...]}, ...]"""
        if not rows:
            return 0
        params = [
            (r["id"], r["question"], _vec_literal(r["embedding"]) if r.get("embedding") else None)
            for r in rows
        ]
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    f"""
                    INSERT INTO {self.table} (id, question, embedding)
                    VALUES (%s, %s, %s::vector)
                    ON CONFLICT (id) DO UPDATE SET
                        question = EXCLUDED.question,
                        embedding = EXCLUDED.embedding,
                        updated_at = now()
                    """,
                    params,
                )
            return len(params)
        except Exception:  # noqa: BLE001
            self._discard()
            raise

    def search(self, embedding: list[float], top_k: int = 10) -> list[tuple[str, float]]:
        """返回 [(example_id, 余弦相似度)]，按相似度降序。"""
        lit = _vec_literal(embedding)
        rows = self._exec(
            f"""
            SELECT id, 1 - (embedding <=> %s::vector) AS score
            FROM {self.table}
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (lit, lit, top_k),
            fetch=True,
        )
        return [(r[0], float(r[1] or 0.0)) for r in (rows or [])]

    def count(self) -> int:
        rows = self._exec(f"SELECT COUNT(*) FROM {self.table}", fetch=True)
        return int(rows[0][0]) if rows else 0
