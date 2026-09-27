"""DBRunner 连接韧性测试：云库空闲回收连接（SSL 已关但 closed 仍为 False）时，
单次查询应自动重连并重跑，而不是把 `SSL connection has been closed unexpectedly`
直接抛给用户（逼其手动重试）。

只 mock 底层 `psycopg.connect`，不连真实库；覆盖两条关键不变量：
  1. 第一次撞上失效连接 -> 自动重连 -> 第二次成功（用户无感）；
  2. 真正的坏 SQL（非连接问题）重试后仍抛错，不会被重试掩盖。
"""
import pytest

import psycopg
from nl2sql.db import PsycopgRunner


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
