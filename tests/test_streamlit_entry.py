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


def _fake_streamlit(engine_label: str, calls: dict | None = None) -> types.ModuleType:
    st = types.ModuleType("streamlit")
    st.secrets = {"LLM__API_KEY": "sk-test", "DB__DSN": "postgresql://u:p@h/db"}
    ss = MagicMock()
    ss.get = lambda *a, **k: None
    ss.pop = lambda *a, **k: False
    st.session_state = ss
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


def _run_entry(monkeypatch, engine_label: str) -> dict:
    calls: dict = {}
    monkeypatch.setitem(sys.modules, "streamlit", _fake_streamlit(engine_label, calls))
    src = ENTRY.read_text(encoding="utf-8")
    exec(compile(src, str(ENTRY), "exec"),
         {"__file__": str(ENTRY), "__name__": "__main__"})
    return calls


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

    calls = _run_entry(monkeypatch, "🔎 DSL · Elasticsearch")
    assert any("尚未配置" in e for e in calls.get("errors", [])), calls.get("errors")


def test_entry_exposes_three_engine_options():
    labels = _engine_labels()
    assert len(labels) == 3
    assert any("SQL" in x for x in labels)
    assert any("DSL" in x for x in labels)
    assert any("PPL" in x for x in labels)
