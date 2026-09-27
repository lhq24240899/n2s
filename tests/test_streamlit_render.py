"""用官方 `streamlit.testing.v1.AppTest` 做**真实渲染**测试。

为什么还需要这一层（已有假 streamlit 的入口冒烟测试）：
    假替身只能看到"调用了哪些 widget"，看不到 Streamlit 自己的**执行顺序**语义。
    本轮「对话区示例问题」就踩了一个只有真实渲染才暴露的坑：
      · 示例块若放在 `st.chat_input` **之后** —— 按钮返回值要到脚本末尾才产生，
        于是点了示例当轮不提问，用户感觉"点了没反应"（要再操作一次才问）；
      · 示例块放在**之前**则相反：点击当轮 turns 还是空的，示例会与刚问出的答案同屏出现。
    最终解是「放在之前 + 问完 st.rerun() 一次」，这两条不变量只有 AppTest 能证实，
    因此这里的测试专门盯它们（LLM 引擎被替换成假返回，测试不联网、不调模型）。
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

pytest.importorskip("streamlit.testing.v1")
from streamlit.testing.v1 import AppTest  # noqa: E402

from nl2sql.models import GenerationResult, ResultSource  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "streamlit_app.py"

SQL_LABEL = "🔢 SQL · PostgreSQL"
ES_LABEL = "🔎 DSL · Elasticsearch"


def _samples(at) -> list[str]:
    return [b.label for b in at.button if b.key and b.key.startswith("sample_")]


@pytest.fixture
def fake_sql_engine(monkeypatch):
    """把 GRGQueryEngine.ask 换成固定返回，避免测试里真调 LLM/真查库。"""
    from examples.grg_engine import GRGQueryEngine

    def fake_ask(self, question, *a, **kw):
        return {
            "type": "result",
            "mapped": types.SimpleNamespace(metric=None, reasons=["（测试替身）"],
                                            entities={"region": "华东"}, normalized=question),
            "result": GenerationResult(sql="SELECT 42 AS answer", raw="SELECT 42 AS answer",
                                       source=ResultSource.LLM),
            "cols": ["answer"],
            "rows": [(42,)],
            "row_count": 1,
            "empty": False,
        }

    monkeypatch.setattr(GRGQueryEngine, "ask", fake_ask)
    return fake_ask


def test_app_renders_without_exception_and_shows_three_samples(fake_sql_engine):
    """应用能在真实 Streamlit 运行时正常渲染，且新对话给出 3 条示例。"""
    at = AppTest.from_file(str(ENTRY), default_timeout=120)
    at.run()

    assert not at.exception, [e.value for e in at.exception]
    assert len(_samples(at)) == 3


def test_clicking_a_sample_asks_immediately_and_hides_the_samples(fake_sql_engine):
    """点示例：**当轮就提问**（不是等下次交互），并且示例区同轮消失。

    这两条正是"位置放错"时会坏掉的地方：
      · 放到 chat_input 之后 -> 当轮 turns 为空（点了没反应）；
      · 不重跑             -> 示例与刚问出的答案同屏。
    """
    at = AppTest.from_file(str(ENTRY), default_timeout=120)
    at.run()
    assert _samples(at), "前置条件：新对话应有示例"

    at.button(key="sample_sql_0").click().run()

    turns = at.session_state["turns_by_engine"]["sql"]
    assert not at.exception, [e.value for e in at.exception]
    assert len(turns) == 2, f"点示例后应当轮完成一问一答，实际 {turns}"
    assert turns[0]["content"] == "华东区上个月可靠性试验的准时完成率是多少"
    assert turns[-1]["role"] == "assistant"
    assert _samples(at) == [], "第一轮完成后示例区应消失"


def test_samples_come_back_after_clearing_the_conversation(fake_sql_engine):
    """清空当前档对话（= 新对话）后，示例重新出现。"""
    at = AppTest.from_file(str(ENTRY), default_timeout=120)
    at.run()
    at.button(key="sample_sql_0").click().run()
    assert _samples(at) == []

    [b for b in at.button if b.label and "清空" in b.label][0].click().run()

    assert at.session_state["turns_by_engine"]["sql"] == []
    assert len(_samples(at)) == 3


def test_each_engine_renders_its_own_three_samples(fake_sql_engine):
    """三档各自渲染 3 条**本档**示例（切档不会串）。"""
    for label, mode in ((SQL_LABEL, "sql"), (ES_LABEL, "es")):
        at = AppTest.from_file(str(ENTRY), default_timeout=120)
        at.run()
        at.radio(key="engine_label_v4").set_value(label).run()

        shown = _samples(at)
        assert not at.exception, (mode, [e.value for e in at.exception])
        assert len(shown) == 3, (mode, shown)
        # 示例必须与当前档匹配：SQL 档问业务指标，日志档问事件流
        if mode == "sql":
            assert any("准时" in s or "通过率" in s or "17025" in s for s in shown), shown
        else:
            assert all("告警" in s or "ERROR" in s or "日志" in s for s in shown), shown


def test_switching_engine_shows_that_engines_own_history(fake_sql_engine):
    """切档只看到该档自己的对话（历史隔离在真实运行时也成立）。"""
    at = AppTest.from_file(str(ENTRY), default_timeout=120)
    at.run()
    at.button(key="sample_sql_0").click().run()
    assert len(at.session_state["turns_by_engine"]["sql"]) == 2

    at.radio(key="engine_label_v4").set_value(ES_LABEL).run()

    assert at.session_state["turns_by_engine"]["es"] == []
    assert len(_samples(at)) == 3, "ES 档是它自己的新对话，应展示示例"


def test_context_fallback_is_disclosed_in_the_ui(monkeypatch):
    """空结果回退必须在界面上讲清楚（否则用户会以为口径被悄悄改了）。"""
    from examples.grg_engine import GRGQueryEngine

    def fake_ask(self, question, *a, **kw):
        return {
            "type": "result",
            "mapped": types.SimpleNamespace(
                metric=None, reasons=["空结果回退: 不使用上一轮继承的过滤维度（区域/业务线/时间）"],
                entities={"business_line": "emc"}, normalized=question,
                inherited_dimensions=["metric"],
            ),
            "result": GenerationResult(sql="SELECT 0.75 AS on_time_rate", raw="s",
                                       source=ResultSource.LLM),
            "cols": ["on_time_rate"],
            "rows": [(0.75,)],
            "row_count": 1,
            "empty": False,
            "context_fallback": {"dropped": ["region", "time"], "normalized": question},
        }

    monkeypatch.setattr(GRGQueryEngine, "ask", fake_ask)
    at = AppTest.from_file(str(ENTRY), default_timeout=120)
    at.run()
    at.button(key="sample_sql_0").click().run()

    assert not at.exception, [e.value for e in at.exception]
    warnings = " ".join(w.value for w in at.warning)
    assert "已忽略这些继承维度重新查询" in warnings
    assert "区域" in warnings and "时间" in warnings      # 维度用中文讲清楚，不暴露内部键名


PPL_LABEL = "🧭 PPL · OpenSearch"
ES_LABEL = "🔎 DSL · Elasticsearch"


def test_ppl_mode_shows_only_ppl_query(monkeypatch):
    """方案 B：PPL 档只展示 PPL 的查询语句，不显示 DSL（DSL 作为对照）。"""
    from examples.es_engine import EsQueryEngine
    from nl2sql import es_backend

    def fake_ask(self, question, *a, **kw):
        return {
            "type": "result",
            "engine": "es",
            # DSL 执行结果：故意和 PPL 不同，用于断言前端真的按 mode 选了 PPL 结果
            "columns": ["region_dsl"],
            "rows": [("华东_dsl",)],
            "es_dsl": {"query": {"bool": {"filter": {"term": {"mode": "dsl"}}}}},
            "ppl": {
                "query": "source=device_events | where level='ERROR' | stats count() as cnt by region",
                "status": "executed",
                "columns": ["region_ppl"],
                "rows": [("华北_ppl",)],
                "adaptations": ["相对时间 -> 绝对时间"],
            },
            "entities": {"index": "device_events", "filters": [("level", "term", "ERROR")]},
            "reasons": ["IR->ES DSL 编译", "PPL: executed"],
            "row_count": 1,
            "empty": False,
        }

    # 避免测试环境缺 ES__PPL_HOST 导致 get_es_engine 返回 None
    monkeypatch.setattr(es_backend, "build_es_backend", lambda settings, mode: object())
    monkeypatch.setattr(EsQueryEngine, "ask", fake_ask)

    at = AppTest.from_file(str(ENTRY), default_timeout=120)
    at.run()
    at.radio(key="engine_label_v4").set_value(PPL_LABEL).run()
    at.button(key="sample_ppl_0").click().run()

    assert not at.exception, [e.value for e in at.exception]

    # 结果区应展示 PPL 行（而不是 DSL 行）：执行状态 caption 里应包含 PPL 与行数
    status_captions = [c.value for c in at.caption if "PPL" in c.value and "1 行" in c.value]
    assert status_captions, "应出现 PPL 执行状态 caption"

    # 只应出现 PPL 的查询语句，不应出现 DSL JSON
    # 过滤掉侧边栏“部署诊断”里的 config_diag() 代码块（配置未就绪时默认展开）
    code_blocks = [c.value for c in at.code if "APP_BUILD=" not in c.value]
    assert code_blocks, "应有查询语句 code 块"
    assert len(code_blocks) == 1, f"PPL 档只应展示 1 个查询语句块: {code_blocks}"
    assert code_blocks[0].startswith("source="), f"PPL 语句应唯一展示: {code_blocks}"
    assert '"mode": "dsl"' not in "\n".join(code_blocks), "PPL 档不应出现 DSL 语句"


def test_es_mode_shows_only_dsl_query(monkeypatch):
    """方案 B 对称验证：DSL 档只展示 DSL 的查询语句，不显示 PPL。"""
    from examples.es_engine import EsQueryEngine
    from nl2sql import es_backend

    def fake_ask(self, question, *a, **kw):
        return {
            "type": "result",
            "engine": "es",
            "columns": ["region_dsl"],
            "rows": [("华东_dsl",)],
            "es_dsl": {"query": {"bool": {"filter": {"term": {"mode": "dsl"}}}}},
            "ppl": {
                "query": "source=device_events | where level='ERROR'",
                "status": "executed",
                "columns": ["region_ppl"],
                "rows": [("华北_ppl",)],
                "adaptations": [],
            },
            "entities": {"index": "device_events", "filters": []},
            "reasons": ["IR->ES DSL 编译"],
            "row_count": 1,
            "empty": False,
        }

    monkeypatch.setattr(es_backend, "build_es_backend", lambda settings, mode: object())
    monkeypatch.setattr(EsQueryEngine, "ask", fake_ask)

    at = AppTest.from_file(str(ENTRY), default_timeout=120)
    at.run()
    at.radio(key="engine_label_v4").set_value(ES_LABEL).run()
    at.button(key="sample_es_0").click().run()

    assert not at.exception, [e.value for e in at.exception]

    code_blocks = [c.value for c in at.code if "APP_BUILD=" not in c.value]
    assert code_blocks, "应有查询语句 code 块"
    assert len(code_blocks) == 1, f"DSL 档只应展示 1 个查询语句块: {code_blocks}"
    assert '"mode": "dsl"' in code_blocks[0], f"DSL JSON 应唯一展示: {code_blocks}"
    assert not code_blocks[0].startswith("source="), "DSL 档不应出现 PPL 语句"


def test_es_answer_uses_collapsible_query_and_mapping_expanders(monkeypatch):
    """ES/PPL 档的「查询语句」与「语义映射（溯源性）」各为独立下拉框，
    对齐 SQL 档「🔍 生成的 SQL」「🧭 语义映射（可解释）」的结构，不再平铺 caption。"""
    from examples.es_engine import EsQueryEngine
    from nl2sql import es_backend

    def fake_ask(self, question, *a, **kw):
        return {
            "type": "result",
            "engine": "es",
            "columns": ["region_dsl"],
            "rows": [("华东_dsl",)],
            "es_dsl": {"query": {"bool": {"filter": {"term": {"mode": "dsl"}}}}},
            "ppl": {"query": "source=device_events | where level='ERROR'",
                    "status": "executed", "columns": [], "rows": [], "adaptations": []},
            "entities": {"index": "device_events", "filters": [("level", "term", "ERROR")]},
            "reasons": ["IR->ES DSL 编译", "多轮上下文继承: level"],
            "row_count": 1,
            "empty": False,
        }

    monkeypatch.setattr(es_backend, "build_es_backend", lambda settings, mode: object())
    monkeypatch.setattr(EsQueryEngine, "ask", fake_ask)

    at = AppTest.from_file(str(ENTRY), default_timeout=120)
    at.run()
    at.radio(key="engine_label_v4").set_value(PPL_LABEL).run()
    at.button(key="sample_ppl_0").click().run()

    assert not at.exception, [e.value for e in at.exception]
    labels = [ex.label for ex in at.expander]
    assert "🔍 生成的查询语句" in labels, f"应有查询语句下拉框: {labels}"
    assert "🧭 语义映射（可解释）" in labels, f"应有语义映射(溯源性)下拉框: {labels}"



def test_sql_answer_shows_elapsed_time_and_doc_snippets(monkeypatch):
    """SQL 档必须展示耗时（与 ES 档一致），且参考知识库要给出溯源片段（文档正文），
    否则使用者只知道"参考了哪篇标题 + 检索分"，却看不出具体参考了哪段资料。"""
    from examples.grg_engine import GRGQueryEngine
    from types import SimpleNamespace

    def fake_ask(self, question, *a, **kw):
        return {
            "type": "result",
            "mapped": SimpleNamespace(metric=None, reasons=["（测试替身）"],
                                     entities={"region": "华东"}, normalized=question),
            "result": GenerationResult(sql="SELECT 42 AS answer", raw="SELECT 42 AS answer",
                                       source=ResultSource.LLM),
            "cols": ["answer"],
            "rows": [(42,)],
            "row_count": 1,
            "empty": False,
            # 故意带一篇有正文 content 的资料，验证溯源片段会被渲染
            "docs": [
                SimpleNamespace(id="m1", title="检测准时率口径说明",
                                content="准时率 = 按期出具报告数 / 应出具报告总数；"
                                        "按期指出具日不晚于承诺出具日。",
                                source="指标口径库", reasons=["RRF 融合得分 0.05", "LLM 精排 9/10"]),
            ],
        }

    monkeypatch.setattr(GRGQueryEngine, "ask", fake_ask)

    at = AppTest.from_file(str(ENTRY), default_timeout=120)
    at.run()
    at.button(key="sample_sql_0").click().run()

    assert not at.exception, [e.value for e in at.exception]

    # 1) 顶部状态行必须含"耗时"（与 ES 档对齐）
    status = [c.value for c in at.caption if "耗时" in c.value]
    assert status, "SQL 档应展示耗时"
    assert "SQL" in status[0], f"耗时 caption 应标明 SQL 引擎: {status}"

    # 2) 参考知识库下拉框里必须出现文档正文（溯源片段），而不只是标题
    md = [m.value for m in at.markdown]
    assert any("准时率 = 按期出具报告数" in m for m in md), \
        f"参考知识库应展示溯源片段(文档正文): {md}"

    # 3) 参考知识库标题也应保留（st.write 的 **加粗** 文本在 AppTest 里归到 markdown）
    assert any("检测准时率口径说明" in m for m in md), "应出现资料标题"
