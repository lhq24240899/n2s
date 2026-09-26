"""导出工具的离线测试（三语言评测集文档生成）。

为什么值得测：
  导出文档是给别人**人肉核对**用的，一旦"展示口径"和"判定口径"不一致，人就会得出错误结论。
  本轮真踩到过：`_fmt_es` 对所有单行结果统一取末列，于是「告警最多的区域是哪个」在文档里
  显示成数字 `60`（而判定用的是首列名称、用例其实是 PASS 的）——文档看起来像失败。
  所以这里把"展示必须与 compare 的取值口径一致"钉住。
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _tool():
    spec = importlib.util.spec_from_file_location(
        "export_eval_set", ROOT / "examples" / "export_eval_set.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_top_kind_display_uses_name_column_not_value():
    """top 类（「最多的区域是哪个」）展示必须取名称列 —— 与 compare 的 rows[0][0] 一致。"""
    tool = _tool()
    rows = [("华东", 60)]
    assert tool._fmt_es(rows, "top") == "华东"
    # scalar 类则取值列，两种口径不能混
    assert tool._fmt_es([(137,)], "scalar") == "137"


def test_distribution_display_pairs_key_and_value():
    tool = _tool()
    out = tool._fmt_es([("华东", 42), ("华南", 30), ("华北", 25)], "rows")
    assert out == "华东 → 42；华南 → 30；华北 → 25"


def test_es_case_kinds_are_known():
    """ES_CASES 的 kind 只允许 scalar / rows / top（导出与判定都依赖它）。"""
    from examples.es_eval import ES_CASES

    assert ES_CASES, "ES 评估集不应为空"
    assert {c["kind"] for c in ES_CASES} <= {"scalar", "rows", "top"}
    for c in ES_CASES:
        assert c["id"] and c["question"] and "expected" in c
