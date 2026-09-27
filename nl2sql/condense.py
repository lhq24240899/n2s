"""追问改写（condense question）：把「指代型追问」补成一句自包含问句。

为什么需要这一层
----------------
多轮**不是**靠模型记忆上下文，而是把历史压成**一句自包含问题**再走单轮链路
（`nl2sql/llm.py:54-57` 只发 system + user 两条消息；`Text2SQLPipeline.query()` 只收一个字符串）。
但"压"这件事原本只覆盖 `metric/region/business_line/time` 四个槽位，而**指代型追问的锚点
在上一轮的结果行里**：

    用户：已开票合同金额最高的客户是哪个   ->  答「某汽车客户」
    用户：金额是多少                       ->  锚点是「某汽车客户」

槽位继承给不出这个锚点，于是问题被悄悄**放大**成"全公司合同金额"——
真机实测：SQL 退化成 `SELECT SUM(amount) FROM contracts`，返回全库 5,450,000，
而正确答案是那个客户的 2,540,000。这类"答非所问却看着像答案"比报错更危险。

为什么交给模型、而不是再写一堆规则
----------------------------------
"它/那个/金额是多少"指代谁，是**语言理解**，静态规则只能靠堆词表硬猜（且会不断误伤）。
但也不能把整条链路交给模型：那样会丢掉可解释性与可复现性（这个项目的卖点是对得上账）。

所以本模块的边界只有一句话：**模型只负责把追问补成自包含问句**，
补完**照旧**交给确定性 mapper（`nl2sql/semantic.py`）做同义词/指标/维度/时间映射。
于是：改写本身仍以一条 reason 显示在「语义映射」面板里，评估集也照样可复现。

触发条件（见 `is_weak_followup`）：**本轮信号弱 + 存在上一轮问题**。
有强信号（指标/分组/计数/排名/任一维度取值）时一律不改写——改写只补指代，不做别的。
"""
from __future__ import annotations

import re
from typing import Optional

CONDENSE_SYSTEM_PROMPT = (
    "你是多轮问句改写器。把用户这一轮的追问改写成**一句自包含的完整问句**："
    "补上被省略的主语/对象（例如「那它呢」「金额是多少」里省略的是上一轮结果中的哪个对象）。"
    "只输出改写后的问句本身，不要解释、不要加引号、不要写 SQL。"
    "若这一轮本身已经自包含，就原样输出。"
)

# 结果里**不应**出现的强信号：有任何一个都说明本轮已经说清了，不需要改写
_STRONG_KEYS = (
    "group_by", "count", "topn", "knowledge",
    "region", "business_line", "customer", "time",
)

# 模型偶尔会带上前缀/引号，甚至跑偏去写 SQL —— 都要在入口挡掉
_LINE_PREFIX_RE = re.compile(
    r"^(?:(?:改写后的|改写后|改写的|改写)?(?:问句|问题|句子|答案|结果)|改写)\s*[:：]\s*"
)
_QUOTES = "\"'“”‘’《》「」 \t\r\n"
_SQL_WORDS = ("select ", "select(", " from ", " group by", "order by", "where ")


def is_weak_followup(mapped) -> bool:
    """本轮信号是否弱到"需要外部补锚点"。

    弱 = 没有指标、没有分组、没有计数/排名/知识型意图、也没有任何维度取值。
    这正好对应"用户没说清在问什么"的追问（"金额是多少""那它呢""继续"）。
    """
    if mapped.metric is not None:
        return False
    return not any(mapped.entities.get(k) for k in _STRONG_KEYS)


def build_condense_prompt(
    prev_question: str,
    follow_up: str,
    prev_result: str = "",
) -> str:
    """拼改写提示词。三项都显式给：上一轮**原始问题**、上一轮结果摘要、本轮追问。"""
    parts = [f"# 上一轮问题\n{prev_question}"]
    if prev_result:
        parts.append(f"# 上一轮的结果\n{prev_result}")
    parts.append(f"# 这一轮的追问\n{follow_up}")
    parts.append("# 任务\n把它改写成一句自包含的问题（只输出这一句）。")
    return "\n\n".join(parts)


def clean_condensed(text: str, *, max_chars: int = 60) -> str:
    """清洗模型输出：去掉前缀/引号/多余行；为空、过长、或看起来是 SQL 的，一律判为不可用。

    返回空串 = "这次改写不可用"，调用方据此**保持原行为**（宁可澄清，也不乱猜）。
    """
    s = (text or "").strip()
    if not s:
        return ""
    s = s.splitlines()[0].strip()
    s = _LINE_PREFIX_RE.sub("", s).strip()
    s = s.strip(_QUOTES)
    if not s or len(s) > max_chars:
        return ""
    low = s.lower()
    if any(w in low for w in _SQL_WORDS):
        return ""
    return s


def condense_followup(
    llm,
    prev_question: str,
    follow_up: str,
    prev_result: str = "",
    *,
    max_chars: int = 60,
) -> Optional[str]:
    """让模型改写一句；不可用（空/超长/像 SQL/调用失败）统一返回 None。"""
    try:
        raw = llm.generate(
            build_condense_prompt(prev_question, follow_up, prev_result),
            CONDENSE_SYSTEM_PROMPT,
        )
    except Exception:  # noqa: BLE001 - 改写是增强项：失败就按原问题走，绝不影响主流程
        return None
    return clean_condensed(raw, max_chars=max_chars) or None
