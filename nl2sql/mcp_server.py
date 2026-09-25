"""MCP server：把「问数智能体」暴露成 MCP 工具，供 Claude Desktop / IDE / 其它智能体调用。

为什么要做 MCP，而不是只留 HTTP API：
  HTTP API 是"给前端用的"；MCP 是"给**智能体**用的"——
  它把能力以「工具 + 说明」的形式注册给宿主模型，模型自己决定何时调用。
  这是当前 agent 生态里事实标准的对接方式（IDE、桌面客户端、编排框架都支持）。

安全模型（与 HTTP API 完全一致，不因为换了入口就放松）：
  - **只暴露只读工具**：这里根本没有写操作工具，MCP 侧就构不成写风险；
  - 角色由启动参数/环境变量固定（默认 analyst），令牌可选用 `--token` 注入；
  - 触发数据权限：无权表在生成前就被摘掉，行级过滤注入进 SQL；
  - 最底层还有数据库会话级只读兜底。

启动：
    python -m nl2sql.mcp_server                   # stdio（IDE / Claude Desktop 用）
    python -m nl2sql.mcp_server --transport sse --port 8765
    python -m nl2sql.mcp_server --role admin --sub ops

Claude Desktop / IDE 配置示例（mcp.json）：
    {
      "mcpServers": {
        "nl2sql": {
          "command": "python",
          "args": ["-m", "nl2sql.mcp_server"],
          "cwd": "<项目根目录>",
          "env": {"NL2SQL__ROLE": "analyst"}
        }
      }
    }
"""
from __future__ import annotations

import argparse
import logging
from typing import Any, Optional

from .auth import Authenticator, MissingSecret, Principal, Role
from .config import get_settings
from .service import QueryService

_log = logging.getLogger("nl2sql.mcp")

INSTRUCTIONS = """计量检测业务问数助手（只读）。

适用场景：
- 业务指标问数：准时完成率、检测一次通过率、设备利用率、检测服务收入、报告出具周期等。
- 支持多轮追问（如先问"华东区准时完成率"，再问"那华南区呢"）。
- 支持按维度分组（"各业务线的准时率"），会返回多行。
- 概念/口径/标准类问题（如"EMC 是什么""为什么问华南区查不到数据"）走知识库检索，回答带引用来源。

使用建议：
1. 先调用 list_metrics / list_tables 了解可用指标与字段，再提问，命中率更高。
2. 同一会话请复用 session_id，否则多轮上下文不连续。
3. 结果里的 reasons / data_scope 是"为什么这么算、受什么权限约束"的依据，回答用户时可一并说明。
4. 本服务只读：任何写入、删除请求都不会被支持，请直接告知用户。
"""


# ---------------------------------------------------------------------------
# 结果格式化：MCP 工具面向模型，用 Markdown 文本最容易被正确理解
# ---------------------------------------------------------------------------

def _format_rows(payload: dict, limit: int = 50) -> str:
    cols = payload.get("columns") or []
    rows = payload.get("rows") or []
    if not cols:
        return "（无数据）"
    head = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join(["---"] * len(cols)) + " |"
    body = [
        "| " + " | ".join("-" if v is None else str(v) for v in r) + " |"
        for r in rows[:limit]
    ]
    tail = f"\n\n（共 {len(rows)} 行，仅展示前 {limit} 行）" if len(rows) > limit else ""
    return "\n".join([head, sep, *body]) + tail


def format_answer(payload: dict) -> str:
    kind = payload.get("type")
    scope = payload.get("data_scope") or []

    if kind == "denied":
        return f"⛔ 拒绝：{payload.get('denied_reason')}\n（{payload.get('detail', '')}）"

    if kind == "clarification":
        return f"❓ 需要澄清：{payload.get('answer')}"

    if kind == "rag":
        parts = [payload.get("answer") or "（无回答）", "", "参考来源："]
        for i, c in enumerate(payload.get("citations") or [], 1):
            parts.append(f"[{i}] {c['title']}（{c.get('source') or '内部语料'}）")
        return "\n".join(parts)

    # result
    parts = []
    if payload.get("metric"):
        parts.append(f"**指标**：{payload['metric']}")
    if payload.get("error"):
        parts.append(f"⚠️ {payload['error']}")
    if payload.get("empty"):
        parts.append("⚠️ 查询已执行但未返回数据（过滤条件可能与库内取值不一致）")
    parts.append(_format_rows(payload))
    if payload.get("sql"):
        parts.append(f"\n```sql\n{payload['sql']}\n```")
    if scope:
        parts.append("数据权限：" + "；".join(scope))
    reasons = payload.get("reasons") or []
    if reasons:
        parts.append("口径依据：" + "；".join(reasons[:6]))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 工具注册
# ---------------------------------------------------------------------------

def create_mcp(service: QueryService, principal: Principal, name: str = "nl2sql-问数"):
    """构建 MCP server。principal 决定这个 server 实例的可见数据范围。"""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(name, instructions=INSTRUCTIONS)

    @mcp.tool()
    def ask_business_question(question: str, session_id: str = "mcp") -> str:
        """用自然语言查询计量检测业务数据（只读）。

        支持：指标问数（准时率/一次通过率/设备利用率/收入/报告周期）、
        区域与业务线筛选、多轮追问、按维度分组；也可回答口径与概念类问题（带引用）。

        Args:
            question: 中文自然语言问题，例如"华东区上个月可靠性试验的准时完成率是多少"。
            session_id: 会话 ID；同一串追问请复用同一个值，默认 "mcp"。
        """
        payload = service.ask(question, principal, session_id=session_id)
        return format_answer(payload)

    @mcp.tool()
    def list_metrics() -> str:
        """列出当前身份**可见**的全部业务指标（含口径定义与可用维度）。

        提问前先看这个，能显著提高命中率（知道指标的标准叫法）。
        """
        cat = service.schema_catalog(principal)
        lines = [f"# 可用指标（角色 {cat['role']}，共 {len(cat['metrics'])} 个）"]
        for m in cat["metrics"]:
            lines.append(
                f"- **{m['name']}**（id={m['id']}，{m['level']}/{m['domain']}）\n"
                f"  口径：{m['definition']}\n"
                f"  维度：{'、'.join(m['dimensions']) or '—'}"
            )
        return "\n".join(lines)

    @mcp.tool()
    def list_tables() -> str:
        """列出当前身份可见的表与字段（敏感字段会被标记）。"""
        cat = service.schema_catalog(principal)
        lines = [f"# 可见表（共 {len(cat['tables'])} 张）"]
        for t in cat["tables"]:
            cols = "、".join(
                f"{c['name']}{'🔒' if c['sensitive'] else ''}" for c in t["columns"]
            )
            lines.append(f"- **{t['name']}**：{t['description']}\n  字段：{cols}")
        lines.append("\n数据权限：" + "；".join(cat["data_scope"]))
        return "\n".join(lines)

    @mcp.tool()
    def get_table_schema(table: str) -> str:
        """查看单张表的完整字段说明。

        Args:
            table: 表名，如 reports / labs / equipment。
        """
        cat = service.schema_catalog(principal)
        for t in cat["tables"]:
            if t["name"] == table:
                cols = "\n".join(
                    f"  - {c['name']} {c['type']}{'（敏感，禁止查询）' if c['sensitive'] else ''}"
                    for c in t["columns"]
                )
                return f"# {t['name']}\n{t['description']}\n字段：\n{cols}"
        names = [t["name"] for t in cat["tables"]]
        return f"未找到表 {table}，或你的角色无权访问。可见表：{names}"

    @mcp.tool()
    def search_knowledge(query: str, top_k: int = 4) -> str:
        """检索企业内部知识库（指标口径、业务术语、检测标准、常见问题）。

        适合回答"XX 是什么意思""为什么…查不到""口径怎么定的"这类问题。

        Args:
            query: 检索关键词或问题。
            top_k: 返回条数，默认 4。
        """
        out = service.kb_search(query, principal, top_k=max(1, min(top_k, 10)))
        hits = out.get("hits") or []
        if not hits:
            return f"未检索到与「{query}」相关的资料。{out.get('note') or ''}"
        lines = [f"# 知识库检索：{query}（{len(hits)} 条）"]
        for i, h in enumerate(hits, 1):
            lines.append(
                f"[{i}] **{h['title']}**（来源：{h.get('source') or '内部语料'}）\n"
                f"{h['content'][:600]}\n  召回依据：{'；'.join(h['reasons'][:3])}"
            )
        return "\n\n".join(lines)

    @mcp.tool()
    def reset_session(session_id: str = "mcp") -> str:
        """清空指定会话的多轮上下文（换话题时调用，避免沿用上一轮的口径）。

        Args:
            session_id: 要重置的会话 ID。
        """
        service.reset(session_id, principal)
        return f"会话 {session_id} 已重置。"

    return mcp


def build_principal(settings, role: str, sub: str, token: Optional[str] = None) -> Principal:
    if token:
        try:
            return Authenticator(settings.auth).principal(token)
        except MissingSecret as e:
            raise SystemExit(
                f"使用了 --token 但没有配置校验密钥：{e}\n"
                "请在环境变量设置 AUTH__SECRET（与签发令牌时一致）。"
            ) from e
    return Principal.of(sub, Role(role))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="NL2SQL MCP server（只读）")
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--role", default=Role.ANALYST.value, choices=[r.value for r in Role])
    parser.add_argument("--sub", default="mcp-client", help="主体标识（审计用）")
    parser.add_argument("--token", default=None, help="可选：直接给 JWT，身份与数据范围以令牌为准")
    parser.add_argument("--orchestrator", default="pipeline", choices=["pipeline", "graph"])
    args = parser.parse_args(argv)

    settings = get_settings()
    principal = build_principal(
        settings, role=args.role, sub=args.sub,
        token=args.token or __import__("os").environ.get("NL2SQL__TOKEN"),
    )

    from .bootstrap import build_service

    service, _ctx = build_service(settings, orchestrator=args.orchestrator)
    mcp = create_mcp(service, principal)

    _log.info(
        "MCP server 启动: transport=%s role=%s sub=%s",
        args.transport, principal.role.value, principal.sub,
    )
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        # 不同版本 FastMCP 的 host/port 放置位置不同，做一次兼容处理
        try:
            mcp.settings.host = args.host
            mcp.settings.port = args.port
        except Exception:  # noqa: BLE001
            pass
        mcp.run(transport=args.transport)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
