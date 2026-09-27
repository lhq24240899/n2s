"""PostgreSQL 长连接的韧性：TCP 保活 + 僵尸连接自动重连。

为什么单独抽一层？

云库（Neon 等）在计算实例空闲约 5 分钟后会挂起，**挂起时服务端直接关掉连接**；
而 psycopg 的 `conn.closed` 仍然是 `False` —— 客户端要等到真正 `execute` 才发现
SSL 已被对端关闭，报：

    consuming input failed: SSL connection has been closed unexpectedly

更值得记的是工程上的坑：项目里有**三条**长连接——
① SQL 预检/执行（`PsycopgRunner`）、② 文档三路召回（`PgVectorStore`）、
③ 示例向量召回（`PgExampleVectorIndex`）。
重连逻辑若只加在其中一条上，另外两条照旧炸；真机踩过，而且炸的恰好是
**每次提问最先用到的 ②③**（文档召回在生成 SQL 之前就跑了），
所以症状是"空闲之后第一问必现，手动再点一次就好"。

把「重连」收成这一个函数、三条连接共用，以后再加连接也不会漏。

只对**只读幂等**语句重试：重跑不产生副作用。
"""
from __future__ import annotations

from typing import Callable, TypeVar

T = TypeVar("T")

# 每条语句最多尝试次数：首次 + 一次重试。
# 重试一次足够——坏连接被丢弃后必然重建，第二次不会是同一条死连接；
# 再多只是把"库真的挂了"的错误延迟暴露。
ATTEMPTS = 2

# TCP 保活：让内核定期探活，缩短「对端已关、本地仍以为活着」的窗口。
# 这是**治因**（降低撞上僵尸连接的频率），真正兜底的仍是下面的自动重连。
# 注：libpq 在 Windows 上会忽略 keepalives_idle（改用系统固定值），不会报错。
KEEPALIVE = {
    "keepalives": 1,
    "keepalives_idle": 30,      # 空闲 30s 起开始探测
    "keepalives_interval": 10,  # 探测间隔 10s
    "keepalives_count": 3,      # 连续 3 次无响应即判定连接已死
}


def run_with_reconnect(
    connect: Callable[[], object],
    discard: Callable[[], None],
    run: Callable[[object], T],
) -> T:
    """执行 `run(conn)`；撞上失效连接就丢弃、重连、再跑一次。

    - `connect()` 自身抛错也在重试范围内（重建可能因库暂时不可达而失败）；
    - 两次都失败时抛**最后一次**的原异常 —— 真正的坏 SQL 不会被重试掩盖。
    """
    last_exc: Exception | None = None
    for _ in range(ATTEMPTS):
        try:
            return run(connect())
        except Exception as e:  # noqa: BLE001 - 连接类异常五花八门，一律重试一次
            last_exc = e
            discard()
    assert last_exc is not None  # 循环至少执行一次，下面必然非空
    raise last_exc
