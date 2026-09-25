"""计量检测 · 智能问数系统 —— Streamlit 入口（部署时的 Main file）。

页面显示名称统一由下方 `DISPLAY_NAME` 常量控制：改那一行即可整体换名。

部署到 Streamlit Community Cloud：
  1. 仓库：https://github.com/lhq24240899/n2s
  2. Main file path：streamlit_app.py
  3. App → Settings → Secrets 填入（键名与 .env 一致，用 TOML）：
        LLM__BASE_URL = "https://api.ephone.ai/v1"
        LLM__API_KEY  = "sk-..."
        LLM__MODEL    = "gpt-4o-mini"
        DB__DSN       = "postgresql://user:pass@host/db?sslmode=require"
     （详见 .streamlit/secrets.toml.example）
  4. 数据库需先建表灌数：本地跑一次 `python examples/setup_dev_db.py`
     （数据写入 Neon，云端直接复用，无需在 Cloud 上再建）

本地运行：
    streamlit run streamlit_app.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 保证 `nl2sql` / `examples` 可被导入（Streamlit 从仓库根启动，通常已满足）
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

# ============================================================
# 页面显示的机构名称（想换名只改这一行；不涉及任何业务逻辑）
# ============================================================
DISPLAY_NAME = "test"

st.set_page_config(
    page_title=f"{DISPLAY_NAME} · 智能问数", page_icon="📊", layout="wide"
)

SOURCE_LABEL = {
    "llm": "✅ LLM 生成 · 通过静态校验 + 执行预检",
    "fallback_template": "🟡 重试耗尽 · 回退到最相关示例 SQL",
    "fallback_generic": "🟠 检索无命中 · 通用兜底生成",
}

# 侧边栏推荐问题：均为「真跑过、确认有返回数据」的问题，保证演示不冷场。
# 前 6 条走结构化问数（SQL），后 2 条走知识库问答（RAG）——两条分支都能演示。
EXAMPLES = [
    "华东区上个月可靠性试验的准时完成率是多少",
    "那华南区呢？",
    "各业务线的检测准时率是多少",
    "华东区上个月可靠性试验的检测服务收入是多少",
    "各实验室设备利用率",
    "可靠性业务的报告出具周期是多少天",
    "为什么问华南区查不到数据",
    "EMC 是什么意思",
]


# ---------------------------------------------------------------------------
# 1) 把 Streamlit secrets 注入环境变量，供 pydantic-settings 读取
#    （Cloud 上没有 .env；本地仍可用 .env / .streamlit/secrets.toml）
# ---------------------------------------------------------------------------
def _flatten(d, prefix=""):
    out: dict = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, f"{key}__"))
        else:
            out[key] = v
    return out


def _inject_secrets() -> None:
    try:
        sec = dict(st.secrets)
    except Exception:
        return
    if not sec:
        return
    for k, v in _flatten(sec).items():
        os.environ.setdefault(k.upper(), str(v))


_inject_secrets()


# ---------------------------------------------------------------------------
# 2) 引擎：每个浏览器会话一个（自带多轮上下文），重组件在该会话内只建一次
# ---------------------------------------------------------------------------
def get_engine():
    if "engine" in st.session_state:
        return st.session_state.engine

    from examples.grg_engine import GRGQueryEngine
    from examples.grg_schema import build_registry, build_semantic_layer, build_store
    from nl2sql.config import get_settings, setup_logging
    from nl2sql.db import build_db
    from nl2sql.llm import build_llm
    from nl2sql.pipeline import Text2SQLPipeline

    settings = get_settings()
    setup_logging(settings.log.level, settings.log.fmt)

    registry = build_registry(settings.db.dialect)
    llm = build_llm(settings.llm)  # 缺 LLM__API_KEY 会直接抛错
    store = build_store()

    st.session_state.engine = GRGQueryEngine(
        Text2SQLPipeline(
            registry=registry,
            store=store,
            llm=llm,
            db=build_db(settings.db, registry),  # 缺 DB__DSN 会直接抛错
            retriever=_build_retriever(settings, store, llm),
            top_k=settings.retrieval.top_k,
            min_score=settings.retrieval.min_score,
            max_retry=settings.pipeline.max_retry,
        ),
        build_semantic_layer(),
        doc_retriever=_build_doc_retriever(settings, llm),
        doc_max_chars=settings.kb.doc_max_chars,
    )
    return st.session_state.engine


def _build_embedder(settings):
    """向量化器；构建失败返回 None（上层自动降级为纯标签检索/纯 SQL 问数）。"""
    try:
        from nl2sql.embedding import build_embedder

        return build_embedder(settings.embedding, settings.llm)
    except Exception:  # noqa: BLE001
        return None


def _build_retriever(settings, store, llm):
    """SQL 示例检索：标签分 ⊕ 示例向量召回（RRF 融合）。未装向量库时自动退化为纯标签。"""
    from nl2sql.retrieval import build_retriever

    embedder = _build_embedder(settings) if settings.kb.enabled else None
    return build_retriever(settings, store, embedder)


def _build_doc_retriever(settings, llm):
    """企业知识库混合检索器（pgvector + pg_trgm + 关键词 → RRF → LLM 精排）。

    构建失败不影响主流程：自动降级为纯 SQL 问数。
    """
    if not settings.kb.enabled:
        return None
    try:
        from nl2sql.kb import build_doc_retriever

        return build_doc_retriever(settings, _build_embedder(settings), llm)
    except Exception:  # noqa: BLE001
        return None


def config_status() -> tuple[bool, str]:
    """启动自检：明确告知缺了哪个配置，而不是等第一次提问才报错。"""
    try:
        from nl2sql.config import get_settings

        s = get_settings()
    except Exception as e:  # noqa: BLE001
        return False, f"配置加载失败：{e}"
    missing = []
    if not s.llm.api_key:
        missing.append("LLM__API_KEY")
    if not s.db.dsn:
        missing.append("DB__DSN")
    if missing:
        return False, "缺少配置：" + "、".join(missing)
    return True, "配置就绪"


SELFCHECK_TABLES = [
    "labs",
    "business_lines",
    "trust_orders",
    "reports",
    "equipment",
    "test_records",
]


def datasource_selfcheck() -> tuple[str, dict]:
    """自检：当前 App 实际连的是哪台库、关键表有没有数据。

    很多「查不到数据」其实是 App 连到了另一个空库，或种子数据没灌进去。
    """
    engine = get_engine()
    db = engine.pipeline.db
    dsn = getattr(db, "dsn", "") or ""
    host = dsn.split("@")[-1].split("/")[0] if "@" in dsn else "(未知)"
    counts: dict = {}
    for t in SELFCHECK_TABLES:
        try:
            _, rows = db.execute(f"SELECT COUNT(*) FROM {t}")
            counts[t] = rows[0][0]
        except Exception as e:  # noqa: BLE001
            counts[t] = f"ERROR: {e}"
    return host, counts


# ---------------------------------------------------------------------------
# 3) 结果渲染
# ---------------------------------------------------------------------------
def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def render_rag(out: dict) -> None:
    """文档问答（混合 RAG）：只依据知识库资料作答，并列出引用来源。"""
    st.caption("📚 知识库问答 · 结构化问数不适用时走文档检索（三路召回 + RRF 融合）")
    st.markdown(out.get("answer") or "（未生成回答）")
    docs = out.get("docs") or []
    if docs:
        with st.expander(f"📚 参考来源（知识库命中 {len(docs)} 篇）"):
            for i, d in enumerate(docs, start=1):
                src = f" — {d.source}" if getattr(d, "source", "") else ""
                st.write(f"**[{i}] {d.title}**{src}")
                if getattr(d, "reasons", None):
                    st.caption("　检索依据：" + d.reasons[0])


def render_doc_sources(docs: list) -> None:
    """结构化问数时展示"知识库参考了哪些资料"（混合 RAG 的另一半）。"""
    if not docs:
        return
    with st.expander(f"📚 参考知识库（{len(docs)} 篇，已作为口径补充注入生成）"):
        for i, d in enumerate(docs, start=1):
            st.write(f"**[{i}] {d.title}**")
            if getattr(d, "reasons", None):
                st.caption("　检索依据：" + d.reasons[0])


def render_answer(out: dict) -> None:
    # 歧义澄清：不进入生成
    if out.get("type") == "clarification":
        st.warning(f"❓ 需要澄清：{out['message']}")
        return

    # 文档问答（RAG 分支）
    if out.get("type") == "rag":
        render_rag(out)
        return

    res = out["result"]
    mapped = out["mapped"]
    cols, rows = out["cols"], out["rows"]

    st.caption(SOURCE_LABEL.get(res.source.value, res.source.value))

    # 空结果判定：无行 或 全部为 NULL
    is_empty = (not rows) or all(v is None for r in rows for v in r)

    # 单值 -> 大数字卡片；多行 -> 表格
    if rows and len(rows) == 1 and len(rows[0]) == 1:
        label = mapped.metric.name if mapped.metric else (cols[0] if cols else "结果")
        if rows[0][0] is None:
            st.warning(
                "查询已执行成功，但**无匹配数据**：过滤条件与库内取值可能不一致"
                "（如区域/业务线的写法）。可换个说法，或确认维度取值后重试。"
            )
        else:
            st.metric(label=label, value=_fmt(rows[0][0]))
    elif rows:
        st.dataframe([dict(zip(cols, r)) for r in rows])
    else:
        st.info("查询执行完成，但无数据返回。")

    if res.error:
        st.error(f"告警：{res.error}")

    # 口径说明（可审计）
    if mapped.metric:
        m = mapped.metric
        st.markdown(
            f"> **口径说明**：{m.name} = {m.definition}  \n"
            f"> 数据来源：`{'`、`'.join(m.source_tables)}`"
        )

    if is_empty and res.sql:
        # 没数据时自动展开 SQL + 诊断信息（本轮过滤实体 / 库内行数），便于直接排查
        st.caption("⬇️ 未返回数据，已自动展开生成的 SQL 与诊断信息：")
        st.code(res.sql, language="sql")
        st.caption(f"本轮识别实体：`{mapped.entities}`")
        try:
            _host, counts = datasource_selfcheck()
            st.caption(
                "当前库核心表行数：" + " · ".join(f"{k} {v}" for k, v in counts.items())
            )
        except Exception:  # noqa: BLE001
            pass
    else:
        with st.expander("🔍 生成的 SQL"):
            st.code(res.sql or "（无）", language="sql")

    with st.expander("🧭 语义映射（可解释）"):
        for r in mapped.reasons:
            st.write(f"- {r}")
        st.write(f"- 归一化问题：`{mapped.normalized}`")
        st.write(f"- 解析实体：`{mapped.entities}`")

    render_doc_sources(out.get("docs") or [])


# ---------------------------------------------------------------------------
# 4) 页面
# ---------------------------------------------------------------------------
st.title(f"📊 {DISPLAY_NAME} · 自然问数系统")
st.caption(
    "语义层驱动的 NL2SQL ⊕ 企业知识库混合 RAG：可解释检索 · Schema Linking · "
    "sqlglot 校验 · EXPLAIN 预检 · 多轮上下文 · 失败回退"
)

ok, msg = config_status()

with st.sidebar:
    st.subheader("⚙️ 状态")
    (st.success if ok else st.error)(msg)

    st.divider()
    st.subheader("💡 示例问题")
    for i, q in enumerate(EXAMPLES):
        if st.button(q, key=f"ex_{i}"):
            st.session_state.pending = q

    st.divider()
    if st.button("🧹 清空对话 / 重置多轮上下文"):
        st.session_state.turns = []
        if "engine" in st.session_state:
            st.session_state.engine.reset_context()
        st.rerun()

    st.divider()
    if st.button("🔌 数据源自检"):
        st.session_state.do_selfcheck = True
    if st.session_state.pop("do_selfcheck", False):
        try:
            host, counts = datasource_selfcheck()
            st.caption(f"当前连接：`{host}`")
            st.dataframe({"表": list(counts), "行数": [str(v) for v in counts.values()]})
        except Exception as e:  # noqa: BLE001
            st.error(f"自检失败：{e}")

    st.caption("数据源：Neon PostgreSQL（需先用 setup_dev_db.py 建表灌数）")

if "turns" not in st.session_state:
    st.session_state.turns = []

# 渲染历史
for t in st.session_state.turns:
    with st.chat_message(t["role"], avatar=("🧑" if t["role"] == "user" else "📊")):
        if t["role"] == "user":
            st.write(t["content"])
        else:
            render_answer(t["payload"])

# 取输入（支持侧边栏示例按钮注入）
question = st.chat_input("用一句话提问，例如：华东区上个月可靠性试验的准时完成率是多少")
if st.session_state.get("pending"):
    question = st.session_state.pop("pending")

if question:
    st.session_state.turns.append({"role": "user", "content": question})
    with st.chat_message("user", avatar="🧑"):
        st.write(question)

    with st.chat_message("assistant", avatar="📊"):
        out = None
        with st.spinner("语义映射 → 检索 → 生成 → 校验 → 预检 → 执行 ..."):
            try:
                out = get_engine().ask(question)
            except Exception as e:  # noqa: BLE001
                st.error(f"初始化或查询失败：{e}")
        if out is not None:
            render_answer(out)
            st.session_state.turns.append({"role": "assistant", "payload": out})
