"""DB 连接韧性测试：云库空闲回收连接（SSL 已关但 closed 仍为 False）时，
单次查询应自动重连并重跑，而不是把 `SSL connection has been closed unexpectedly`
直接抛给用户（逼其手动重试）。

只 mock 底层 `psycopg.connect`，不连真实库；覆盖关键不变量：
  1. 第一次撞上失效连接 -> 自动重连 -> 第二次成功（用户无感）；
  2. 真正的坏 SQL（非连接问题）重试后仍抛错，不会被重试掩盖；
  3. **三条连接都有重试、都有 TCP 保活**——真机踩过"只给 SQL 执行那条加了重试，
     知识库两条照旧炸"，而后者恰是一次提问里最先用到的。这组测试就是防它复发。
"""
import pytest

import psycopg
from nl2sql.db import PsycopgRunner
from nl2sql.vectorstore import PgExampleVectorIndex, PgVectorStore


def _make_runner(monkeypatch, cursor_factory):
    """返回一个 PsycopgRunner，其底层连接由 cursor_factory 决定每次 execute 的行为。"""

    class Conn:
        closed = False

        def cursor(self):
            return cursor_factory()

        def close(self):
            pass

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: Conn())
    # readonly=False / statement_timeout_ms=0：避免 _connect 里额外的 SET 调用干扰 mock
    return PsycopgRunner(dsn="postgresql://u:p@h/db", readonly=False, statement_timeout_ms=0)


def test_execute_recovers_from_stale_connection_by_reconnecting(monkeypatch):
    """僵尸连接（首查即报 SSL closed）应自动重连并重跑成功。"""
    n = {"q": 0}

    def cursor():
        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, *a, **k):
                n["q"] += 1
                if n["q"] == 1:
                    # 第一次：连接已被服务端悄悄关闭，execute 才暴露
                    raise psycopg.OperationalError(
                        "SSL connection has been closed unexpectedly"
                    )

            def fetchall(self):
                return [(0.75,)]

            @property
            def description(self):
                return [("on_time_rate",)]

        return Cur()

    runner = _make_runner(monkeypatch, cursor)
    cols, rows = runner.execute("SELECT 0.75 AS on_time_rate")

    assert rows == [(0.75,)]
    assert cols == ["on_time_rate"]
    assert n["q"] >= 2, "应触发一次重试（首查失败 -> 重连 -> 再查成功）"


def test_bad_sql_still_raises_after_retry(monkeypatch):
    """坏 SQL（非连接问题）重试两次后仍抛错，不被重试逻辑吞掉。"""

    def cursor():
        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, *a, **k):
                raise psycopg.OperationalError("relation \"nope\" does not exist")

            @property
            def description(self):
                return None

        return Cur()

    runner = _make_runner(monkeypatch, cursor)
    with pytest.raises(Exception):
        runner.execute("SELECT * FROM nope")


# ---------------- 知识库两条连接（_PgConnMixin） ----------------
# PgVectorStore（文档三路召回）与 PgExampleVectorIndex（示例向量召回）是一次提问里
# **最先**用到的两条连接：文档召回在生成 SQL 之前就跑（grg_engine.py:208）。
# 它们此前没有重试，所以云库挂起后的第一问必现 SSL 报错，且手动重问就好。


def _patch_connect(monkeypatch, on_execute):
    """把 psycopg.connect 换成假连接；on_execute 在每次 cur.execute 时被调用。"""

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=()):
            on_execute()

        def fetchall(self):
            return [(7,)]

    class Conn:
        closed = False  # 模仿真机：服务端已关掉连接，客户端 closed 仍是 False

        def cursor(self):
            return Cur()

        def close(self):
            pass

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: Conn())


@pytest.mark.parametrize("store_cls", [PgVectorStore, PgExampleVectorIndex])
def test_kb_connections_recover_from_stale_connection(monkeypatch, store_cls):
    """僵尸连接（首查即报 SSL closed）应自动重连并重跑成功。"""
    n = {"q": 0}

    def on_execute():
        n["q"] += 1
        if n["q"] == 1:
            raise psycopg.OperationalError(
                "consuming input failed: SSL connection has been closed unexpectedly"
            )

    _patch_connect(monkeypatch, on_execute)
    store = store_cls(dsn="postgresql://u:p@h/db")

    assert store.count() == 7
    assert n["q"] >= 2, "应重连后重跑一次（首查撞僵尸连接 -> 重连 -> 再查成功）"


def test_kb_bad_sql_still_raises_after_retry(monkeypatch):
    """坏 SQL（非连接问题）重试后仍抛错，不被重试逻辑吞掉。"""

    def on_execute():
        raise psycopg.errors.UndefinedTable('relation "nope" does not exist')

    _patch_connect(monkeypatch, on_execute)
    store = PgVectorStore(dsn="postgresql://u:p@h/db")
    with pytest.raises(psycopg.errors.UndefinedTable):
        store.count()


# ---------------- TCP 保活（治因） ----------------
# 保活不能替代重连，但能缩短「对端已关、本地仍以为活着」的窗口，
# 从源头减少撞上僵尸连接的次数。三条连接必须一致，别漏。

KEEPALIVE_EXPECTED = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}


def _capturing_connect(captured):
    def fake_connect(*a, **kw):
        captured.update(kw)

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=()):
                pass

            def fetchall(self):
                return [(7,)]

        class Conn:
            closed = False

            def cursor(self):
                return Cur()

            def close(self):
                pass

        return Conn()

    return fake_connect


@pytest.mark.parametrize("store_cls", [PgVectorStore, PgExampleVectorIndex])
def test_kb_connections_enable_tcp_keepalive(monkeypatch, store_cls):
    captured: dict = {}
    monkeypatch.setattr(psycopg, "connect", _capturing_connect(captured))

    store_cls(dsn="postgresql://u:p@h/db").count()

    for key, want in KEEPALIVE_EXPECTED.items():
        assert captured.get(key) == want, f"{store_cls.__name__} 缺少保活参数 {key}"


def test_db_runner_enables_tcp_keepalive(monkeypatch):
    """SQL 执行层同样带保活参数——三条连接行为一致。"""
    captured: dict = {}
    monkeypatch.setattr(psycopg, "connect", _capturing_connect(captured))

    # readonly=False / statement_timeout_ms=0：避免 _connect 里额外的 SET 调用
    PsycopgRunner(
        dsn="postgresql://u:p@h/db", readonly=False, statement_timeout_ms=0
    )._connect()

    for key, want in KEEPALIVE_EXPECTED.items():
        assert captured.get(key) == want, f"PsycopgRunner 缺少保活参数 {key}"
