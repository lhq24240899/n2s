"""MCP server 测试：工具注册 + 工具输出 + 数据权限在 MCP 入口同样生效。

MCP SDK 未安装时整体跳过（保持测试可在最小依赖下运行）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("mcp", reason="未安装 mcp SDK")

from nl2sql.auth import Principal, Role
from nl2sql.mcp_server import create_mcp, format_answer

from tests.service_factory import make_service, make_settings


def _text(result) -> str:
    """把 call_tool 的返回值压成纯文本（不同版本返回 shape 略有差异）。"""
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, list):
        return "\n".join(getattr(c, "text", None) or str(c) for c in result)
    return str(result)


def _call(mcp, name: str, args: dict) -> str:
    return _text(asyncio.run(mcp.call_tool(name, args)))


@pytest.fixture()
def mcp_env():
    settings = make_settings()
    service = make_service(settings, with_kb=True)
    principal = Principal.of("mcp-client", Role.ANALYST)
    return create_mcp(service, principal), service, principal


def test_tools_registered(mcp_env):
    mcp, _, _ = mcp_env
    tools = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {
        "ask_business_question",
        "list_metrics",
        "list_tables",
        "get_table_schema",
        "search_knowledge",
        "reset_session",
    } <= tools
    # 只读：绝不能存在写操作工具
    assert not any(k in t for t in tools for k in ("write", "insert", "delete", "update", "drop"))


def test_ask_tool_returns_markdown_table(mcp_env):
    mcp, _, _ = mcp_env
    out = _call(mcp, "ask_business_question", {"question": "华东区上个月可靠性试验的准时完成率是多少"})
    assert "指标" in out and "on_time_rate" in out
    assert "```sql" in out  # 附带 SQL，便于人工核对


def test_ask_tool_multi_turn(mcp_env):
    mcp, _, _ = mcp_env
    _call(mcp, "ask_business_question", {"question": "华东区上个月可靠性试验的准时完成率是多少"})
    out = _call(mcp, "ask_business_question", {"question": "那华南区呢？", "session_id": "mcp"})
    assert "0.887" in out  # 华南（替身固定值），说明多轮上下文在 MCP 入口同样生效


def test_list_metrics_tool(mcp_env):
    mcp, _, _ = mcp_env
    out = _call(mcp, "list_metrics", {})
    assert "可用指标" in out
    assert "on_time_completion_rate" in out
    assert "口径" in out


def test_list_tables_and_get_table_schema(mcp_env):
    mcp, _, _ = mcp_env
    tables = _call(mcp, "list_tables", {})
    assert "reports" in tables and "数据权限" in tables
    one = _call(mcp, "get_table_schema", {"table": "reports"})
    assert "issued_at" in one
    missing = _call(mcp, "get_table_schema", {"table": "not_exist"})
    assert "未找到表" in missing


def test_search_knowledge_tool(mcp_env):
    mcp, _, _ = mcp_env
    out = _call(mcp, "search_knowledge", {"query": "EMC 是什么"})
    assert "知识库检索" in out and "召回依据" in out  # 可解释性一并透出


def test_reset_session_tool(mcp_env):
    mcp, _, _ = mcp_env
    _call(mcp, "ask_business_question", {"question": "华东区准时率是多少", "session_id": "s"})
    assert "已重置" in _call(mcp, "reset_session", {"session_id": "s"})


def test_data_policy_applies_on_mcp_entry():
    """数据权限在 MCP 入口同样生效：令牌限定华东 -> SQL 被注入区域过滤。"""
    settings = make_settings()
    service = make_service(settings, with_kb=True)
    principal = Principal.of("east", Role.ANALYST, regions=["华东"])
    mcp = create_mcp(service, principal)
    out = _call(mcp, "ask_business_question", {"question": "各实验室设备利用率"})
    assert "l.region = '华东'" in out
    assert "数据权限" in out


def test_metric_denied_message_on_mcp_entry():
    from nl2sql.policy import DataPolicy

    settings = make_settings()
    service = make_service(
        settings,
        policy_factory=lambda p: DataPolicy(
            allowed_metrics=frozenset({"on_time_completion_rate"}), dialect="postgres"
        ),
    )
    mcp = create_mcp(service, Principal.of("v", Role.ANALYST))
    out = _call(mcp, "ask_business_question", {"question": "华东区上个月可靠性试验的检测服务收入是多少"})
    assert "拒绝" in out and "无权查询指标" in out


def test_format_answer_handles_all_payload_kinds():
    assert "拒绝" in format_answer({"type": "denied", "denied_reason": "无权", "detail": ""})
    assert "澄清" in format_answer({"type": "clarification", "answer": "你是指 A 吗"})
    rag = format_answer({"type": "rag", "answer": "EMC 是……",
                         "citations": [{"title": "术语-EMC", "source": "内部语料"}]})
    assert "参考来源" in rag and "术语-EMC" in rag
