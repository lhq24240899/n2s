"""语义层「资产体检」：示例 SQL / 指标口径模板必须是可解析、可校验、无敏感字段的。

为什么单独建这个文件：
  这些 SQL 会**直接进提示词**（教 LLM 怎么写），也是**回退路径真正执行的语句**。
  写坏一个括号不会让任何单测失败，却会在真机上以"回退执行成功、但结果错/越权"的形式出现。
  本轮审计就踩到过：给 revenue 示例加 DISTINCT 子查询时漏了闭合的 `) x`，
  于是 sqlglot 解析失败 →（当时）列级权限检查静默跳过 → 含被禁字段的 SQL 被直接执行。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import sqlglot

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from examples.grg_schema import build_registry, build_semantic_layer, build_store
from nl2sql.config import Settings
from nl2sql.policy import DataPolicy, PolicyGuard
from nl2sql.validation import SQLValidator


@pytest.fixture(scope="module")
def ctx():
    st = Settings()
    registry = build_registry(st.db.dialect)
    return {
        "dialect": st.db.dialect,
        "registry": registry,
        "store": build_store(),
        "layer": build_semantic_layer(),
        "validator": SQLValidator(registry),
    }


def test_every_example_sql_parses(ctx):
    """示例 SQL 必须能被 sqlglot 解析（写坏括号就失败）。"""
    bad = []
    for ex in ctx["store"].all():
        try:
            sqlglot.parse_one(ex.sql, dialect=ctx["dialect"])
        except Exception as e:  # noqa: BLE001
            bad.append(f"{ex.id}: {type(e).__name__}: {e}")
    assert not bad, "示例 SQL 解析失败：\n" + "\n".join(bad)


def test_every_example_sql_passes_validator(ctx):
    """示例 SQL 还必须通过校验器（表/列都在 registry 里、且无写操作）。"""
    bad = []
    for ex in ctx["store"].all():
        err = ctx["validator"].validate(ex.sql, ctx["registry"].names())
        if err:
            bad.append(f"{ex.id}: {err}")
    assert not bad, "示例 SQL 未通过校验：\n" + "\n".join(bad)


def test_every_metric_hint_parses_and_validates(ctx):
    """指标口径模板（进提示词的 SQL 参考）同样要能解析 + 过校验。"""
    bad = []
    for m in ctx["layer"].metrics.values():
        hint = getattr(m, "sql_hint", "") or ""
        if not hint.strip():
            continue
        try:
            sqlglot.parse_one(hint, dialect=ctx["dialect"])
        except Exception as e:  # noqa: BLE001
            bad.append(f"{m.id} 解析失败: {e}")
            continue
        err = ctx["validator"].validate(hint, ctx["registry"].names())
        if err:
            bad.append(f"{m.id} 未过校验: {err}")
    assert not bad, "指标模板有问题：\n" + "\n".join(bad)


def test_example_sql_returns_no_denied_columns_under_default_policy(ctx):
    """默认策略（敏感字段封禁）下，示例 SQL 不应引用被禁字段——否则回退就是越权。"""
    guard = PolicyGuard(DataPolicy(dialect=ctx["dialect"]))
    leaked = [ex.id for ex in ctx["store"].all()
              if guard._find_denied_column(ex.sql)]
    assert not leaked, f"示例 SQL 含敏感字段，回退会越权：{leaked}"


def test_denied_column_check_fails_closed_on_unparseable_sql(ctx):
    """列级权限检查必须 fail-closed：SQL 解析不了时**拒绝**，而不是放行。

    早期实现"解析失败就跳过检查、交给 validator"，可回退路径不一定调用 validator，
    于是语法坏的 SQL 能连语法带权限一起绕过。
    """
    guard = PolicyGuard(DataPolicy(denied_columns=frozenset({"amount"}), dialect=ctx["dialect"]))
    assert guard._find_denied_column("SELECT SUM(x.amount) FROM (") is not None
    assert guard._find_denied_column("SELECT amount FROM contracts") == "amount"
    # 没配敏感字段时不因解析失败而误伤
    open_guard = PolicyGuard(DataPolicy(denied_columns=frozenset(), dialect=ctx["dialect"]))
    assert open_guard._find_denied_column("SELECT SUM(x.amount) FROM (") is None
