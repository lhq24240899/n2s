"""Streamlit 入口的「整文件导入」冒烟测试（用假的 streamlit 模块）。

为什么值得单独测：
  `streamlit_app.py` 是**脚本式**入口——侧边栏、聊天区、提问分支都在模块顶层执行，
  所以"某个分支只在切换到某个引擎档位时才走到"这种情况，单测以外的静态检查完全看不出来。
  本轮就真踩到过两个：
    ① 部署环境探测候选路径时 `Path.exists()` 抛 PermissionError（诊断面板自己把应用搞崩）；
    ② `es_backend_info()` 用了只在别的函数里局部导入的 `resolve_index`
       —— 配了 ES 之后切到 DSL/PPL 档直接 NameError（此前没配 ES，走的是报错分支，掩盖了）。
  因此这里用假 streamlit 把入口**完整执行一遍**，并按档位参数化，确保每条分支都真的能跑通。

假 streamlit 的要点：
  - `session_state` 用 MagicMock 并覆写 get/pop（否则 `.get()` 返回 Mock 真值会误入提问分支）；
  - `button` 一律 False、`chat_input` 返回 None —— 保证只渲染、不触发查询；
  - `sequence`/`expander` 返回 MagicMock 以支持 `with` 上下文。
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "streamlit_app.py"


class _SessionState(dict):
    """最小可用的 session_state 替身：支持下标访问 + 属性访问（像真 Streamlit 一样）。"""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as e:  # 属性不存在时抛 AttributeError，而非 KeyError
            raise AttributeError(name) from e

    def __setattr__(self, name, value):
        self[name] = value


def _fake_streamlit(engine_label: str, calls: dict | None = None,
                    session=None) -> types.ModuleType:
    st = types.ModuleType("streamlit")
    st.secrets = {"LLM__API_KEY": "sk-test", "DB__DSN": "postgresql://u:p@h/db"}
    st.session_state = session if session is not None else _SessionState()
    st.radio = lambda *a, **k: engine_label
    st.chat_input = lambda *a, **k: None
    st.button = lambda *a, **k: False
    st.sidebar = MagicMock()
    st.set_page_config = lambda *a, **k: None
    st.expander = lambda *a, **k: MagicMock()
    st.chat_message = lambda *a, **k: MagicMock()
    log = calls if calls is not None else {}
    for name in ("title", "caption", "subheader", "divider", "write", "success", "code",
                 "dataframe", "rerun", "metric", "info", "warning", "markdown", "spinner"):
        setattr(st, name, lambda *a, **k: None)
    st.error = lambda *a, **k: log.setdefault("errors", []).append(a[0] if a else "")
    return st


def _run_entry(monkeypatch, engine_label: str, session=None) -> tuple[dict, dict, dict]:
    """执行入口，返回 (widget 调用记录, session_state, 入口模块的全局命名空间)。"""
    calls: dict = {}
    session = session if session is not None else _SessionState()
    monkeypatch.setitem(sys.modules, "streamlit", _fake_streamlit(engine_label, calls, session))
    src = ENTRY.read_text(encoding="utf-8")
    g: dict = {"__file__": str(ENTRY), "__name__": "__main__"}
    exec(compile(src, str(ENTRY), "exec"), g)
    return calls, session, g


def _engine_labels() -> list[str]:
    """从入口源码里取三档 radio 的选项文案（避免测试与实现各写一份而漂移）。"""
    import ast

    tree = ast.parse(ENTRY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "ENGINE_OPTIONS":
            return [k.value for k in node.value.keys]
    raise AssertionError("入口里找不到 ENGINE_OPTIONS")


@pytest.mark.parametrize("label", _engine_labels())
def test_entry_imports_cleanly_in_every_engine_mode(monkeypatch, label):
    """每个引擎档位下完整执行入口都不得抛异常。"""
    _run_entry(monkeypatch, label)


def test_dsl_mode_without_config_reports_error_instead_of_crashing(monkeypatch):
    """未配置 ES 时切到 DSL 档：应给出可读提示，而不是抛异常。"""
    monkeypatch.setenv("ES__ENABLED", "false")
    monkeypatch.setenv("ES__HOST", "")
    monkeypatch.setenv("ES__PPL_ENABLED", "false")
    monkeypatch.setenv("ES__PPL_HOST", "")

    calls, _, _ = _run_entry(monkeypatch, "🔎 DSL · Elasticsearch")
    assert any("尚未配置" in e for e in calls.get("errors", [])), calls.get("errors")


def test_entry_exposes_three_engine_options():
    labels = _engine_labels()
    assert len(labels) == 3
    assert any("SQL" in x for x in labels)
    assert any("DSL" in x for x in labels)
    assert any("PPL" in x for x in labels)


# ---------------- 对话历史按引擎隔离 ----------------

def test_conversation_history_is_isolated_per_engine(monkeypatch):
    """三档各有独立的 turns 列表：往 sql 里写不该出现在 es/ppl。"""
    _, session, g = _run_entry(monkeypatch, "🔢 SQL · PostgreSQL")
    assert g["ALL_ENGINE_MODES"] == ("sql", "es", "ppl")

    sql_turns = g["_engine_turns"]("sql")
    sql_turns.append({"role": "user", "content": "华东区准时率"})

    assert [t["content"] for t in g["_engine_turns"]("sql")] == ["华东区准时率"]
    assert g["_engine_turns"]("es") == []
    assert g["_engine_turns"]("ppl") == []
    store = session[g["TURNS_BY_ENGINE_KEY"]]
    assert set(store) == {"sql", "es", "ppl"}


def test_engine_turns_reinitializes_on_bad_state(monkeypatch):
    """session 里存了非 dict（老版本遗留）时，应重建而不是崩掉。"""
    session = _SessionState()
    session["turns_by_engine"] = ["legacy"]
    _, _, g = _run_entry(monkeypatch, "🔢 SQL · PostgreSQL", session=session)
    assert g["_engine_turns"]("es") == []


def test_switching_engine_shows_only_its_own_history(monkeypatch):
    """切换档位后，渲染循环只遍历该档自己的历史。"""
    seen: list[str] = []
    session = _SessionState()

    for label, label_turns in (("🧭 PPL · OpenSearch", "ppl"), ("🔎 DSL · Elasticsearch", "es")):
        _, _, g = _run_entry(monkeypatch, label, session=session)
        # 用同一个 session 连续跑两个档位：各自看到的列表必须互不干扰
        g["_engine_turns"]("sql").append({"role": "user", "content": "只属于 SQL"})
        assert g["_engine_turns"]("sql") != g["_engine_turns"](label_turns)
        seen.append(label_turns)

    assert seen == ["ppl", "es"]
