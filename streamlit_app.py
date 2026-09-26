"""计量检测 · 智能问数系统 —— Streamlit 入口（部署时的 Main file：streamlit_app.py）。

页面显示名称统一由下方 `DISPLAY_NAME` 常量控制：改那一行即可整体换名。

本地运行：
    streamlit run streamlit_app.py

部署到 Streamlit Community Cloud（推荐）：
  1. 把本仓库推到 GitHub（公开或私有均可）。
  2. share.streamlit.io → New app → 选仓库/分支（main）→
     Main file path 填 `streamlit_app.py`。
  3. App → Settings → Secrets 粘贴 TOML（键名与 .env 一致，**不要提交到仓库**）：
       LLM__PROVIDER / LLM__BASE_URL / LLM__API_KEY / LLM__MODEL
       DB__DIALECT / DB__DSN / DB__READONLY / DB__DRY_RUN
     （顶层键会被 Streamlit 自动注入为环境变量；本文件也会再做一次兜底注入。）
  4. 依赖由根目录 requirements.txt 安装（已精简为 Streamlit 运行时所需）。
  - 端口/地址：Streamlit Cloud 自行管理，无需配置；本文件仅在平台注入 $PORT 时才跟随。
  - 输入安全护栏已内置（问 apikey / 注入 / PII 会被直接拒绝，见 nl2sql/safety.py）。
  - 页面侧边栏「🩺 部署诊断」可自查：配置是否就绪、secrets 从哪读到、环境变量是否注入。

（备选）百度 AI Studio highcode：
  - 入口文件名需为 `Streamlit.app.py`，且推送到其应用空间自有仓库；
    把本文件复制一份改名为 `Streamlit.app.py` 即可，其余文件整包拷入。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# 保证 `nl2sql` / `examples` 可被导入（Streamlit 从仓库根启动，通常已满足）
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# 0) 加载本地 .streamlit/secrets.toml 到环境变量（必须在 import streamlit 之前）
#    某些平台（如百度 AI Studio highcode）启动 Streamlit 的工作目录可能不是仓库根目录，
#    导致 Streamlit 原生 secrets 机制找不到文件。这里基于 __file__ 绝对路径手动解析，
#    不依赖 CWD / st.secrets，兼容 Python 3.9+（优先 tomllib，回退简单行解析）。
# ---------------------------------------------------------------------------
def _load_local_secrets() -> None:
    # 候选路径：入口文件所在目录、工作目录、以及常见的平台家目录，
    # 防止 highcode 把仓库存到非预期目录、或用非根 CWD 启动导致找不到 .streamlit/secrets.toml。
    candidates = []
    try:
        candidates.append(Path(__file__).resolve().parent / ".streamlit" / "secrets.toml")
    except NameError:
        pass
    candidates.append(_ROOT / ".streamlit" / "secrets.toml")
    candidates.append(Path.cwd() / ".streamlit" / "secrets.toml")
    for home in ("/home/aistudio", "/home/jovyan", "/app", os.path.expanduser("~")):
        if home:
            candidates.append(Path(home) / ".streamlit" / "secrets.toml")
    # 也允许仓库根直接放 secrets.toml（无 .streamlit 子目录）
    candidates += [c.parent / "secrets.toml" for c in list(candidates)]

    path = None
    for c in candidates:
        try:
            if c and c.exists():
                path = c
                break
        except OSError:
            continue
    if path is None:
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    if not raw.strip():
        return

    # 优先用标准库 tomllib（Python >= 3.11）
    def _setenv(k: str, v: str) -> None:
        # 注意用「空值也覆盖」而不是 setdefault：平台可能注入了 LLM__API_KEY="" 这类空变量，
        # setdefault 会保留空值导致 pydantic 读到空串，报"缺少配置"。
        if not os.environ.get(k):
            os.environ[k] = v

    try:
        import tomllib  # type: ignore
        data = tomllib.loads(raw)
        for k, v in data.items():
            if v is None:
                continue
            if isinstance(v, (str, int, float, bool)):
                _setenv(str(k).upper(), str(v))
        return
    except Exception:
        pass

    # 兼容旧版 Python 的简单行解析（只处理顶层 key = "value" / key = 123 / key = true）
    import re
    for line in raw.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("["):
            continue
        m = re.match(r'^([A-Za-z0-9_-]+)\s*=\s*(.+)$', line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        elif v.lower() in ("true", "false"):
            v = v.lower()
        _setenv(k.upper(), v)


try:
    _load_local_secrets()
except Exception:  # noqa: BLE001
    # 本地 secrets 加载绝不能影响应用启动（Cloud 上密钥走 st.secrets / 平台注入）
    pass

# ---------------------------------------------------------------------------
# 0a) 云端部署端口绑定（必须在 import streamlit 之前设置）
#     百度 AI Studio highcode 等平台一般会把监听端口写在 $PORT 环境变量里；
#     仅当平台显式注入 $PORT 时才覆盖，否则交给 Streamlit / 平台启动命令决定，
#     避免端口和平台 ingress 不一致导致页面打不开。
#     绑定 0.0.0.0 + headless，保证平台能从外部访问到。
# ---------------------------------------------------------------------------
if os.environ.get("PORT"):
    os.environ["STREAMLIT_SERVER_PORT"] = os.environ["PORT"]
os.environ.setdefault("STREAMLIT_SERVER_ADDRESS", "0.0.0.0")
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
os.environ.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
os.environ.setdefault("STREAMLIT_SERVER_ENABLE_CORS", "false")

import streamlit as st

# 再兜底一次：如果平台原生注入 st.secrets（或环境变量），确保 os.environ 里也有。
# 顺序：环境变量 > .streamlit/secrets.toml > st.secrets（st.secrets 不覆盖已有值）。
try:
    for _k in ("LLM__BASE_URL", "LLM__API_KEY", "LLM__MODEL", "LLM__PROVIDER",
               "DB__DSN", "DB__READONLY", "DB__DRY_RUN",
               # 第二/第三执行引擎（ES DSL 真执行 / OpenSearch PPL 真执行）
               "ES__ENABLED", "ES__HOST", "ES__USER", "ES__PASSWORD", "ES__INDEX",
               "ES__PPL_ENABLED", "ES__PPL_HOST", "ES__PPL_USER", "ES__PPL_PASSWORD",
               "ES__PPL_INDEX"):
        _v = st.secrets.get(_k)
        if _v and not os.environ.get(_k):
            os.environ[_k] = str(_v)
except Exception:
    pass

# ============================================================
# 页面显示的机构名称（想换名只改这一行；不涉及任何业务逻辑）
# ============================================================
DISPLAY_NAME = "智能问数"

st.set_page_config(
    page_title=DISPLAY_NAME, page_icon="📊", layout="wide"
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
    # 走知识库（RAG）分支的一条：保持业务口吻（行业标准/资质类真实提问），
    # 不用「XX 是什么意思」「为什么查不到数据」这类系统自述式问句。
    "ISO/IEC 17025 和 GB/T 27025 有什么区别",
]

# ---------------------------------------------------------------------------
# 执行引擎切换：同一个问题，可以分别交给三种真实引擎跑
#   sql  -> Text2SQL（PostgreSQL，语义层 + RAG）
#   es   -> Elasticsearch DSL（QueryIR 编译成 _search 请求体，真执行）
#   ppl  -> OpenSearch PPL（同一份 IR 编译成 PPL，走 _plugins/_ppl 真执行）
# 设计：三种模式共用同一套 IR/语义层与安全护栏——**换引擎不换安全等级**。
# ---------------------------------------------------------------------------
ENGINE_OPTIONS = {
    "🔢 SQL · PostgreSQL": "sql",
    "🔎 DSL · Elasticsearch": "es",
    "🧭 PPL · OpenSearch": "ppl",
}
ENGINE_BADGE = {
    "sql": "SQL（Text2SQL → PostgreSQL）",
    "es": "ES DSL（IR → _search，真执行）",
    "ppl": "PPL（IR → _plugins/_ppl，真执行）",
}

# 对话历史按引擎隔离：三档各有独立的 turns 列表与多轮上下文，
# 切档只看到该引擎自己的对话（否则 SQL 的追问语境会被 ES 档继承走，反之亦然）。
TURNS_BY_ENGINE_KEY = "turns_by_engine"
ALL_ENGINE_MODES = ("sql", "es", "ppl")


def _engine_turns(mode: str) -> list:
    store = st.session_state.get(TURNS_BY_ENGINE_KEY)
    if not isinstance(store, dict):
        store = {m: [] for m in ALL_ENGINE_MODES}
        st.session_state[TURNS_BY_ENGINE_KEY] = store
    return store.setdefault(mode, [])


# ES / OpenSearch 域示例问题（与 examples/es_eval.py 的评估集同源，真跑过有结果）
ES_EXAMPLES = [
    "各区域最近7天的ERROR告警数量",
    "各区域最近30天的ERROR告警数量",
    "最近30天共有多少条ERROR告警",
    "包含「温度超限」的告警最近7天有多少条",
    "各实验室最近7天的ERROR告警数量",
    "告警最多的区域是哪个",
    "最近7天各级别有多少条日志",
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
    from nl2sql.safety import SafetyGuard

    settings = get_settings()
    setup_logging(settings.log.level, settings.log.fmt)

    registry = build_registry(settings.db.dialect)
    layer = build_semantic_layer()
    llm = build_llm(settings.llm)  # 缺 LLM__API_KEY 会直接抛错
    store = build_store()

    st.session_state.engine = GRGQueryEngine(
        Text2SQLPipeline(
            registry=registry,
            store=store,
            llm=llm,
            db=build_db(settings.db, registry),  # 缺 DB__DSN 会直接抛错
            retriever=_build_retriever(settings, store, llm),
            graph=layer.graph,  # 业务知识图谱 -> Schema Linking（中文业务词 -> 物理表）
            top_k=settings.retrieval.top_k,
            min_score=settings.retrieval.min_score,
            max_retry=settings.pipeline.max_retry,
        ),
        layer,
        doc_retriever=_build_doc_retriever(settings, llm),
        doc_max_chars=settings.kb.doc_max_chars,
        # 输入安全护栏：拦截密钥提取 / 提示词注入 / PII / 越界。
        # 不接这一句，部署出去的 Streamlit 会被"问 apikey 是多少"类攻击绕过（直奔 RAG）。
        safety=SafetyGuard(),
    )
    return st.session_state.engine


# ---------------------------------------------------------------------------
# 2b) 第二/第三执行引擎：ES DSL 与 OpenSearch PPL
#     两者共用 EsQueryEngine（确定性规则填槽 → QueryIR → 编译 → REST 真执行），
#     区别只在「指向哪个端点、执行哪种语言」：
#       mode="es"  -> ES__HOST，执行 _search（DSL）；同时把 PPL 作为编译产物一并展示
#       mode="ppl" -> ES__PPL_HOST（回退 ES__HOST），执行 _plugins/_ppl（PPL）
# ---------------------------------------------------------------------------
def get_es_engine(mode: str):
    key = f"es_engine_{mode}"
    if key in st.session_state:
        return st.session_state[key]

    from examples.es_engine import EsQueryEngine
    from nl2sql.config import get_settings
    from nl2sql.es_backend import build_es_backend, resolve_index

    settings = get_settings()
    backend = build_es_backend(settings, mode=mode)
    if backend is None:
        st.session_state[key] = None
        return None
    engine = EsQueryEngine(backend, index=resolve_index(settings, mode))
    st.session_state[key] = engine
    return engine


def es_backend_info(settings, mode: str) -> str:
    """给侧边栏显示：当前模式连的是哪台集群、哪个索引（不显示凭据）。"""
    from nl2sql.es_backend import resolve_index

    if mode == "es":
        host, index = settings.es.host, resolve_index(settings, "es")
    else:
        host = settings.es.ppl_host or settings.es.host
        index = resolve_index(settings, "ppl")
    safe_host = host.split("://")[-1].split("@")[-1] if host else "(未配置)"
    return f"`{safe_host}` · 索引 `{index}`"


def sql_target_info(settings) -> str:
    """给侧边栏显示：SQL 档连的是哪台库、哪个库名、什么方言（不显示账号密码）。"""
    dsn = getattr(getattr(settings, "db", None), "dsn", "") or ""
    if not dsn:
        return "(未配置 DB__DSN)"
    tail = dsn.split("@")[-1].split("?")[0]      # user:pw@ 之后、查询参数之前
    hostport, _, dbname = tail.partition("/")
    dialect = getattr(settings.db, "dialect", "postgres")
    return f"`{hostport}` · 库 `{dbname or '(未指定)'}` · 方言 `{dialect}`"


def render_es_answer(out: dict, mode: str, elapsed_ms: float) -> None:
    """渲染 ES / PPL 结果：结论表 + 真实下发的查询语句 + 执行状态。"""
    if out.get("type") == "refused":
        st.warning(f"🚫 {out.get('answer', '该问题不在我的回答范围内。')}")
        return
    if out.get("type") == "clarification":
        st.warning(f"❓ 需要澄清：{out.get('message')}")
        return
    if out.get("type") == "error":
        st.error(f"查询失败：{out.get('message')}")
        return

    cols, rows = out.get("columns") or [], out.get("rows") or []
    ppl = out.get("ppl") or {}
    st.caption(f"🔧 {ENGINE_BADGE[mode]} · 耗时 {elapsed_ms:.0f} ms · "
               f"{out.get('row_count', len(rows))} 行")

    # 结果区：单值给大数字，多行给表格
    if rows and len(rows) == 1 and len(rows[0]) == 1:
        st.metric(label=cols[0] if cols else "结果", value=_fmt(rows[0][0]))
    elif rows:
        st.dataframe([dict(zip(cols, r)) for r in rows])
    else:
        st.info("查询执行完成，但无数据返回。")

    # 语句区：DSL 与 PPL 都展示 —— 同一份 IR 的两种编译产物，便于对照
    with st.expander("🔍 实际下发的查询语句（可对照 DSL / PPL 两种方言）", expanded=True):
        if out.get("es_dsl"):
            st.markdown("**Elasticsearch DSL**" + ("（本次真执行）" if mode == "es" else ""))
            st.code(json.dumps(out["es_dsl"], ensure_ascii=False, indent=2), language="json")
        if ppl.get("query"):
            status = ppl.get("status")
            label = {"executed": "（本次真执行）", "compiled-only": "（仅编译，未执行）"}.get(status, "")
            st.markdown(f"**PPL** {label}")
            st.code(ppl["query"], language="sql")
            for a in ppl.get("adaptations") or []:
                st.caption(f"⚙️ 编译层适配：{a}")
            if mode == "ppl" and status != "executed":
                st.info(
                    "当前 PPL 未真执行，已降级为「仅编译」。常见原因：\n"
                    "1. 该集群是普通 Elasticsearch，没有 `_plugins/_ppl` 端点（PPL 是 OpenSearch 的语言）；\n"
                    "2. 未配置 `ES__PPL_HOST`（PPL 专用的 OpenSearch 端点）；\n"
                    "3. 端点或鉴权问题，详见下方返回。"
                )
            if ppl.get("status_detail"):
                st.caption(f"PPL 端点返回：{ppl['status_detail']}")

    if out.get("entities"):
        ent = out["entities"]
        st.caption(f"IR 解析：索引 `{ent.get('index')}`，过滤条件 {ent.get('filters')}")
    # 编译/多轮说明（上下文继承、维度替换、PPL 方言适配等）
    for r in out.get("reasons") or []:
        st.caption(f"· {r}")


# ---------------------------------------------------------------------------
# 2c) 统一的「按引擎分发」入口
# ---------------------------------------------------------------------------
def run_es_query(question: str, mode: str) -> tuple[dict, float]:
    """ES / PPL 路径：先过安全护栏，再交给 EsQueryEngine 真执行。

    护栏只做**硬拦截**（密钥提取 / 提示词注入 / PII）：
    越界软拦截的词表是按"计量检测业务"建的，套到事件日志域会误杀
    （例如「各区域 ERROR 告警」这类问句）。换引擎不换安全等级，但档位要跟域匹配。
    """
    from nl2sql.safety import SafetyGuard

    ref = SafetyGuard().screen(question, hard_only=True)
    if ref is not None:
        return {"type": "refused", "answer": ref.safe_reply}, 0.0

    engine = get_es_engine(mode)
    if engine is None:
        return {"type": "error", "message": "该引擎未配置（缺 ES__* / ES__PPL_* 配置）"}, 0.0

    t0 = time.time()
    out = engine.ask(question)
    return out, (time.time() - t0) * 1000


def render_by_engine(payload: dict, mode: str, elapsed_ms: float = 0.0) -> None:
    if mode == "sql":
        render_answer(payload)
    else:
        render_es_answer(payload, mode, elapsed_ms)


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


# 部署自检标记：每次重新部署后改这个值，用户刷新即可判断平台是否拉到了新代码。
APP_BUILD = "2026-09-26-clean-examples"

# secrets.toml 候选路径（与 _load_local_secrets 保持一致，用于诊断显示）
def _secrets_candidates():
    cands = [Path(__file__).resolve().parent / ".streamlit" / "secrets.toml",
             _ROOT / ".streamlit" / "secrets.toml",
             Path.cwd() / ".streamlit" / "secrets.toml"]
    for home in ("/home/aistudio", "/home/jovyan", "/app", os.path.expanduser("~")):
        if home:
            cands.append(Path(home) / ".streamlit" / "secrets.toml")
    return cands


def _safe_exists(p) -> bool:
    """安全的存在性判断：某些平台目录（如 /home/aistudio）无权 stat，
    Path.exists() 会抛 PermissionError（OSError 子类），必须兜住，否则诊断面板本身就崩。"""
    try:
        return bool(p.exists())
    except OSError:
        return False
    except Exception:
        return False


def config_diag() -> str:
    """返回部署环境诊断文本（不泄露密钥值，只显示是否存在/前几位）。"""
    lines = []
    try:
        lines.append(f"APP_BUILD={APP_BUILD}")
    except Exception:
        pass
    try:
        lines.append(f"CWD={Path.cwd()}")
    except Exception:
        lines.append("CWD=<err>")
    try:
        lines.append(f"__file__={Path(__file__).resolve()}")
    except Exception:
        lines.append("__file__=<err>")
    # secrets 文件探测（逐个安全判断，任一目录无权访问都不影响整体）
    found = []
    for p in _secrets_candidates():
        if _safe_exists(p):
            found.append(str(p))
    lines.append(f"secrets.toml 命中: {found if found else '无'}")
    # 平台是否原生注入了 st.secrets（Cloud 的密钥面板走这条路）
    try:
        keys = sorted(str(k) for k in dict(st.secrets).keys())
        lines.append(f"st.secrets 键: {keys if keys else '空'}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"st.secrets 读取失败: {type(e).__name__}")
    # 环境变量状态（只显示是否存在 + 前 6 位，避免泄漏完整密钥）
    for k in ("LLM__BASE_URL", "LLM__API_KEY", "LLM__MODEL", "DB__DSN", "DB__READONLY"):
        v = os.environ.get(k)
        if v:
            lines.append(f"env[{k}] = 已注入 (前6位: {v[:6]}…)")
        else:
            lines.append(f"env[{k}] = 缺失")
    # 最终结论：pydantic 到底能不能读到
    try:
        from nl2sql.config import get_settings

        s = get_settings()
        lines.append(
            f"解析结果: llm.api_key={'有' if s.llm.api_key else '空'}, "
            f"llm.model={s.llm.model}, db.dsn={'有' if s.db.dsn else '空'}"
        )
    except Exception as e:  # noqa: BLE001
        lines.append(f"解析失败: {type(e).__name__}: {e}")
    return "\n".join(lines)


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
    # 安全护栏拒绝：明确告知，不进入任何生成 / 检索
    if out.get("type") == "refused":
        st.warning(f"🚫 {out.get('answer', '该问题不在我的回答范围内。')}")
        return

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
st.title(f"📊 {DISPLAY_NAME}")

ok, msg = config_status()

with st.sidebar:
    st.subheader("⚙️ 状态")
    (st.success if ok else st.error)(msg)
    st.caption(f"构建版本：`{APP_BUILD}`")

    # 部署诊断：配置缺失时默认展开，帮助定位是"代码没更新 / secrets 没读到 / 环境变量没注入"
    with st.expander("🩺 部署诊断", expanded=not ok):
        st.code(config_diag(), language="text")

    st.divider()
    st.subheader("🔀 执行引擎")
    _engine_label = st.radio(
        "执行引擎",
        list(ENGINE_OPTIONS.keys()),
        index=0,
        # key 带版本后缀：改过选项文案后，老浏览器 session 里存着的旧选项值不在新列表里，
        # Streamlit 会直接抛 StreamlitAPIException；换 key 可让旧值自然失效（避免线上白屏）。
        key="engine_label_v4",
        label_visibility="collapsed",
        help="SQL 走 PostgreSQL；DSL 走 Elasticsearch 的 _search；PPL 走 OpenSearch 的 _plugins/_ppl。"
             "三者共用同一份 IR/语义层与安全护栏。",
    )
    ENGINE_MODE = ENGINE_OPTIONS[_engine_label]

    from nl2sql.config import get_settings as _gs

    _s = _gs()
    if ENGINE_MODE == "sql":
        st.caption(f"目标：{sql_target_info(_s)}")
    elif get_es_engine(ENGINE_MODE) is None:
        st.error(
            f"该引擎尚未配置：请在 Secrets / `.env` 里补\n"
            f"`ES__ENABLED=true` + `ES__HOST`（DSL），"
            f"以及 `ES__PPL_ENABLED=true` + `ES__PPL_HOST`（PPL）。"
        )
    else:
        st.caption(f"目标：{es_backend_info(_s, ENGINE_MODE)}")

    st.divider()
    st.subheader("💡 示例问题")
    if ENGINE_MODE == "sql":
        for i, q in enumerate(EXAMPLES):
            if st.button(q, key=f"ex_{i}"):
                st.session_state.pending = q
    else:
        # ES / PPL 域的问题（与 examples/es_eval.py 评估集同源）
        for i, q in enumerate(ES_EXAMPLES):
            if st.button(q, key=f"esex_{i}"):
                st.session_state.pending = q

    st.divider()
    if st.button("🧹 清空当前引擎的对话 / 重置多轮上下文"):
        # 只清当前引擎：历史按引擎隔离，各自的多轮上下文也各自重置
        _engine_turns(ENGINE_MODE).clear()
        if ENGINE_MODE == "sql":
            if "engine" in st.session_state:
                st.session_state.engine.reset_context()
        else:
            _e = st.session_state.get(f"es_engine_{ENGINE_MODE}")
            if _e is not None:
                _e.reset_context()
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

# 渲染历史：只渲染当前引擎自己的对话（历史按引擎隔离）
for t in _engine_turns(ENGINE_MODE):
    with st.chat_message(t["role"], avatar=("🧑" if t["role"] == "user" else "📊")):
        if t["role"] == "user":
            st.write(t["content"])
        else:
            render_by_engine(t["payload"], t.get("engine", ENGINE_MODE), t.get("elapsed_ms", 0.0))

# 取输入（支持侧边栏示例按钮注入）
_PLACEHOLDER = {
    "sql": "用一句话提问，例如：华东区上个月可靠性试验的准时完成率是多少",
    "es": "问事件日志类问题，例如：各区域最近7天的ERROR告警数量",
    "ppl": "问事件日志类问题，例如：各实验室最近7天的ERROR告警数量",
}
question = st.chat_input(_PLACEHOLDER.get(ENGINE_MODE, _PLACEHOLDER["sql"]))
if st.session_state.get("pending"):
    question = st.session_state.pop("pending")

if question:
    _turns = _engine_turns(ENGINE_MODE)
    _turns.append({"role": "user", "content": question})
    with st.chat_message("user", avatar="🧑"):
        st.write(question)

    with st.chat_message("assistant", avatar="📊"):
        out, elapsed_ms = None, 0.0
        _spin = {
            "sql": "语义映射 → 检索 → 生成 → 校验 → 预检 → 执行 ...",
            "es": "规则填槽 → QueryIR → 编译 ES DSL → _search 真执行 ...",
            "ppl": "规则填槽 → QueryIR → 编译 PPL → _plugins/_ppl 真执行 ...",
        }[ENGINE_MODE]
        with st.spinner(_spin):
            try:
                if ENGINE_MODE == "sql":
                    out = get_engine().ask(question)
                else:
                    out, elapsed_ms = run_es_query(question, ENGINE_MODE)
            except Exception as e:  # noqa: BLE001
                st.error(f"初始化或查询失败：{e}")
        if out is not None:
            render_by_engine(out, ENGINE_MODE, elapsed_ms)
            _turns.append(
                {"role": "assistant", "payload": out, "engine": ENGINE_MODE, "elapsed_ms": elapsed_ms}
            )
