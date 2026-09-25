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
    """只读查询执行器（EXPLAIN 预检 + 真实执行）。

    关键设计：连接使用 **autocommit**，每条语句各自独立成事务。

    为什么必须这样？PostgreSQL 里只要某条语句在事务中报错，整个事务会被标记为
    aborted，此后**同一连接上的所有语句都会报**
    `current transaction is aborted, commands ignored until end of transaction block`，
    直到显式 ROLLBACK 为止。

    实测踩过的坑：不设 autocommit 时，某次 LLM 生成了非法 SQL -> 这条连接被毒化 ->
    之后**整个会话所有问题都查不出数据**（表现为"本地跑得好好的、线上全查不到"，
    因为本地每次运行脚本都是新连接，而 Web 端一个会话会长期复用同一条连接）。

    第二道保险：任何异常都会丢弃当前连接，下次调用自动重连。

    只读加固（第三道保险，纵深防御）：
    - `readonly=True` 时连接后执行 `SET default_transaction_read_only = on`，
      此后这条连接上的任何写操作都会被**数据库本身**拒绝
      （报 `cannot execute INSERT in a read-only transaction`）。
      即使某条 SQL 绕过了 sqlglot 的 AST 白名单（例如通过只读视图/函数副作用），
      数据库这一层仍然拦得住——这是权限设计里"不依赖单一防线"的做法。
    - `statement_timeout_ms` 限制单条语句耗时，避免一条慢查询把连接和 LLM 预算占满。
    """

    def __init__(
        self,
        dsn: str,
        dialect: str = "postgres",
        timeout: float = 10.0,
        readonly: bool = True,
        statement_timeout_ms: int = 5000,
    ):
        self.dsn = dsn
        self.dialect = dialect
        self.timeout = timeout
        self.readonly = readonly
        self.statement_timeout_ms = statement_timeout_ms
        self._conn = None

    def _connect(self):
        if self._conn is None or getattr(self._conn, "closed", False):
            import psycopg  # 懒加载，未安装时报清晰错误

            conn = psycopg.connect(
                self.dsn,
                connect_timeout=int(self.timeout),
                autocommit=True,  # 只读场景：避免一条坏语句毒化整个会话
            )
            try:
                with conn.cursor() as cur:
                    if self.readonly:
                        cur.execute("SET default_transaction_read_only = on")
                    if self.statement_timeout_ms:
                        # 注意：SET 语句不接受绑定参数（PG 语法限制，会报 syntax error at "$1"），
                        # 改用 set_config() —— 它是普通函数调用，支持参数化，避免拼接 SQL。
                        cur.execute(
                            "SELECT set_config('statement_timeout', %s, false)",
                            (str(int(self.statement_timeout_ms)),),
                        )
            except Exception:
                conn.close()
                raise
            self._conn = conn
        return self._conn

    def _discard(self) -> None:
        """丢弃当前连接，下一次调用自动重连（不把坏状态的连接留给后续查询）。"""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _query(self, sql: str) -> tuple[list[str], list[tuple]]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                cols = [d[0] for d in cur.description] if cur.description else []
                return cols, cur.fetchall()
        except Exception:  # noqa: BLE001
            self._discard()
            raise

    def explain(self, sql: str) -> tuple[bool, Optional[str]]:
        try:
            self._query(f"EXPLAIN {sql}")  # 只读预检，不产生任何写/读副作用
            return True, None
        except Exception as e:  # noqa: BLE001
            return False, f"EXPLAIN 失败: {e}"

    def execute(self, sql: str) -> tuple[list[str], list[tuple]]:
        return self._query(sql)


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
    return PsycopgRunner(
        settings.dsn,
        dialect=settings.dialect,
        timeout=settings.timeout,
        readonly=getattr(settings, "readonly", True),
        statement_timeout_ms=getattr(settings, "statement_timeout_ms", 5000),
    )
