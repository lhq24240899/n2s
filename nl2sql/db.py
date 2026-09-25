"""DB 执行层：把「执行预检」与「真实执行」抽象出来。

生产用 PsycopgRunner：
- explain 走 PostgreSQL 的 EXPLAIN（只读、不真正执行数据），
  让数据库帮我们做最后一道把关（校验层是静态 AST 检查，仍可能漏掉
  语义合法但运行期才暴露的问题：类型不匹配、权限不足、视图不存在等）。
- execute 走真实游标，返回 (列名, 行数据)。

未配置 DSN 时 `build_db` 直接抛错（不再回退 Mock）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class DBRunner(ABC):
    @abstractmethod
    def explain(self, sql: str) -> tuple[bool, Optional[str]]:
        """执行预检。生产环境为 EXPLAIN / LIMIT 1。返回 (是否通过, 错误信息)。"""

    @abstractmethod
    def execute(self, sql: str) -> tuple[list[str], list[tuple]]:
        """真实执行，返回 (列名列表, 行数据列表)。"""


class PsycopgRunner(DBRunner):
    def __init__(self, dsn: str, dialect: str = "postgres", timeout: float = 10.0):
        self.dsn = dsn
        self.dialect = dialect
        self.timeout = timeout
        self._conn = None

    def _connect(self):
        if self._conn is None:
            import psycopg  # 懒加载，未安装时报清晰错误

            self._conn = psycopg.connect(self.dsn, connect_timeout=int(self.timeout))
        return self._conn

    def explain(self, sql: str) -> tuple[bool, Optional[str]]:
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute(f"EXPLAIN {sql}")  # 只读预检，不产生任何写/读副作用
                cur.fetchall()
            return True, None
        except Exception as e:  # noqa: BLE001
            return False, f"EXPLAIN 失败: {e}"

    def execute(self, sql: str) -> tuple[list[str], list[tuple]]:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        return cols, rows


def build_db(settings, registry=None) -> DBRunner:
    """工厂：配置 DSN 则返回真实 PsycopgRunner；缺 DSN 直接抛错。

    registry 参数保留仅为兼容调用方签名，真实执行不依赖 schema 注册表
    （白名单校验已由 SQLValidator 在生成侧完成）。
    """
    if not settings.dsn:
        raise ValueError(
            "未配置 DB__DSN，无法构建真实 DB 执行器。"
            "请在 .env 中填入 DB__DSN（如 postgresql://user:pwd@host/db）。"
        )
    return PsycopgRunner(settings.dsn, dialect=settings.dialect, timeout=settings.timeout)
