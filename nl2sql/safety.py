"""输入安全护栏：在问题进入检索 / LLM / 数据库之前，筛掉四类危险输入。

为什么放在最前面（pipeline.run 的第一行、service.ask 的入口）？
  NL2SQL 系统的真实风险面不是「生成了错误的 SQL」（那有 validator 兜着），
  而是「被诱导去做它不该做的事」：泄露密钥、被提示词注入劫持、被问一堆跟
  数据无关的问题还硬去生成 SQL。把拦截前置到输入层，成本最低、最干净，也最
  容易被面试时一眼看懂「安全边界画在哪」。

四类拦截（优先级从高到低，命中即拦，不再往下走）：
  1) 指令劫持 / 提示词注入（INJECTION）：硬拦截，无论是否像数据问题。
  2) 密钥 / 凭据 / 系统提示词提取（SECRET）：硬拦截。
  3) 个人敏感信息请求（PII）：硬拦截（手机号 / 身份证 / 邮箱 …）。
  4) 越界 / 无关问题（OUT_OF_SCOPE）：软拦截——只有当问题**完全没有任何**
     「数据域信号」时才拒绝，避免误杀正常问数请求。

设计取舍：
  - 纯规则（正则 + 词表），不依赖 LLM：零延迟、零花费、可离线单测、可解释。
  - 宁可漏放、不可错杀：OUT_OF_SCOPE 只在「无数据信号」时触发；而
    INJECTION / SECRET / PII 是强信号硬拦截，命中即拦。
  - 规则显式陈列，便于审计与面试讲解，而不是塞进黑盒分类器。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class RefusalCategory(str, Enum):
    """拒绝原因分类，便于审计与前端差异化提示。"""

    INJECTION = "injection"        # 指令劫持 / 提示词注入
    SECRET = "secret"              # 密钥 / 令牌 / 系统提示词提取
    PII = "pii"                    # 个人敏感信息（手机号 / 身份证 / 邮箱 …）
    OUT_OF_SCOPE = "out_of_scope"  # 越界 / 与业务数据无关


@dataclass
class Refusal:
    """一次拦截结果：分类 + 内部原因 + 给用户的友好回复（绝不回显敏感内容）。"""

    category: RefusalCategory
    reason: str
    safe_reply: str


# ---------------------------------------------------------------------------
# 检测规则（刻意写得「显式可审计」，而不是塞进一个黑盒分类器）
# ---------------------------------------------------------------------------

# 1) 指令劫持 / 提示词注入：企图覆盖系统指令、冒充身份、诱导执行任意 SQL
_INJECTION_PATTERNS = [
    r"忽略", r"无视", r"忘掉", r"遗忘",                       # 中文：忽略/无视上文
    r"ignore\b", r"disregard\b", r"forget\b",                  # 英文
    r"你现在(不是|不再是|变成|只是)", r"现在你是", r"你现在的(身份|角色|设定)",
    r"假装你是", r"扮演", r"role\s?play", r"you\s+are\s+now",
    r"act\s+as", r"pretend", r"作为一名?",
    r"系统提示", r"你的(指令|提示词|prompt|system)", r"reveal\s+your\s+(instructions|prompt|system)",
    r"把(你(的)?)?(指令|提示词|系统提示)", r"输出你(的)?(系统|内部)",
    r"执行(以下|这条|这个)(sql|语句|查询)", r"run\s+this\s+query", r"execute\s+the\s+following",
]

# 2) 密钥 / 凭据 / 系统配置 / 系统提示词提取
_SECRET_PATTERNS = [
    r"api[_ ]?key", r"apikey", r"密钥", r"密码", r"passwd", r"password",
    r"token", r"令牌", r"凭证", r"凭据", r"secret", r"私钥", r"access\s+key",
    r"连接串", r"\bdsn\b", r"数据库连接", r"数据库密码", r"数据库地址",
    r"\.env", r"环境变量", r"系统配置", r"内部配置", r"账号密码", r"配置(文件|项)",
    r"系统提示", r"你的(提示词|指令)", r"system\s+prompt",
]

# 3) 个人敏感信息请求
_PII_PATTERNS = [
    r"手机号", r"手机", r"身份证", r"住址", r"邮箱", r"e-?mail",
    r"客户(电话|手机|邮箱|地址)", r"联系(方式|电话|人)", r"私人(信息|资料)",
    r"个人隐私",
]

# 4) 数据域信号：只要问题里出现这些词，就认为是「可能的数据问题」，放行。
#    这是 OUT_OF_SCOPE 软拦截的「白名单」——宁可漏放不可错杀。
_DATA_SIGNALS = (
    "多少", "统计", "查询", "查一下", "查", "表", "数据库", "数据",
    "收入", "营收", "销售额", "订单", "用户", "客户", "设备", "实验室", "报告",
    "检测", "计量", "告警", "日志", "区域", "业务线", "同比", "环比", "占比",
    "排名", "最高", "最低", "平均", "总数", "总和", "趋势", "月度", "季度",
    "年度", "昨天", "今天", "最近", "上个月", "上月", "本月", "本年",
    "哪个", "哪些", "分布", "明细", "列表", "导出", "分析", "率", "金额",
    "合同", "检测服务", "出具", "准时", "利用率", "在线", "台数", "数量",
    "各", "对比", "变化", "增长", "下降", "波动",
)
_DATA_SIGNAL_RE = re.compile("|".join(re.escape(s) for s in _DATA_SIGNALS))

# 简单算式（如 "1+1"）：无数据信号时视为无关
_ARITHMETIC_RE = re.compile(r"\d+\s*[+\-*/]\s*\d+")

# 越界话题词（中英文）：命中即判为无关，即便误带一个数据词也优先拒绝。
# 这是 OUT_OF_SCOPE 的「强信号」——兜住用户最在意的「问无关的事」。
_OFFTOPIC_HINTS = [
    "天气", "气温", "下雨", "下雪", "诗歌", "诗", "散文", "小说", "代码", "编程",
    "程序", "翻译", "笑话", "讲个", "你是谁", "你叫", "聊天", "闲聊",
    "推荐(电影|歌|书)", "怎么学习", "学习方法", "数学题", "物理", "化学题",
    "写一篇", "写一首", "总结新闻", "新闻", "股票", "健身", "菜谱", "做饭", "烹饪",
    r"\b(weather|poem|joke|movie|song|translate|math|recipe|novel)\b",
]
_OFFTOPIC_HINTS_RE = re.compile("|".join(_OFFTOPIC_HINTS), re.IGNORECASE)

# 豁免：即便「无数据信号」也不拒绝（让其正常走链路，由 validator/回退兜底）。
# 目的——**不误杀**正常的多轮追问、确认回复、英文问句、问候、纯数字、SQL 注入探测串。
_BENIGN_GREETING_RE = re.compile(r"你好|您好|hi|hello|在吗|早上好|晚上好", re.IGNORECASE)
_BENIGN_SQLI_RE = re.compile(r"('|--|;|\bdrop\b|\bdelete\b|\binsert\b|\bupdate\b)", re.IGNORECASE)
_FOLLOWUP_RE = re.compile(r"呢|那|这个|上一条|继续|还有|其他|同上|接着")
_DIGITS_ONLY_RE = re.compile(r"^[\d\s\W]+$")
# 确认类回复（与 grg_engine.CONFIRM_WORDS 对齐）：多轮澄清时用户回「是的/好的」等，
# 必须放行（只过硬拦截），否则澄清闭环会断。
_CONFIRM_RE = re.compile(
    r"^(是|是的|是的呢|对|对的|嗯|嗯嗯|没错|确定|确认|可以|好|好的|行|没问题|yes|ok|okay|sure|y|yeah)$",
    re.IGNORECASE,
)


class SafetyGuard:
    """输入安全护栏：screen(question) -> Optional[Refusal]。

    返回 None 表示放行；返回 Refusal 表示应直接拒绝，不进入后续链路。
    纯函数、无状态、可离线单测。
    """

    def __init__(self, *, enable_out_of_scope: bool = True):
        self.enable_out_of_scope = enable_out_of_scope

    def screen(self, question: str, *, hard_only: bool = False) -> Optional[Refusal]:
        if not question or not question.strip():
            return Refusal(
                RefusalCategory.OUT_OF_SCOPE,
                "问题为空",
                "我只能回答与计量检测业务数据相关的问题，请描述你想查询的数据。",
            )
        q = question.strip()

        # 优先级 1：指令劫持 / 提示词注入
        hit = self._match(_INJECTION_PATTERNS, q)
        if hit:
            return Refusal(
                RefusalCategory.INJECTION,
                f"检测到疑似提示词注入/指令劫持（命中：{hit}）",
                "我无法执行「忽略系统指令」「冒充身份」或「执行任意 SQL」类请求。"
                "如需查询业务数据，请直接描述你的问题。",
            )

        # 优先级 2：密钥 / 凭据 / 系统提示词提取
        hit = self._match(_SECRET_PATTERNS, q)
        if hit:
            return Refusal(
                RefusalCategory.SECRET,
                f"检测到密钥/凭据提取意图（命中：{hit}）",
                "出于安全合规，我无法提供任何密钥、令牌、密码、数据库连接或系统内部配置。"
                "如需查询业务数据，请描述你的数据问题。",
            )

        # 优先级 3：个人敏感信息请求
        hit = self._match(_PII_PATTERNS, q)
        if hit:
            return Refusal(
                RefusalCategory.PII,
                f"检测到个人敏感信息请求（命中：{hit}）",
                "出于个人信息保护要求，我无法提供手机号、身份证、邮箱等个人敏感信息。",
            )

        # 优先级 4：越界 / 无关问题（软拦截）
        # 判定逻辑（两层，避免误杀正常问数请求与多轮追问）：
        #   a) 命中显式越界话题词（天气/诗歌/电影…）→ 直接拒绝；
        #   b) 既不含数据信号、又不属于「豁免项」→ 拒绝。
        # 豁免项：问候、确认回复、含拉丁字母（英文问句）、纯数字串、SQL 注入探测串、
        #       短句多轮追问（那/呢/继续…）——这些放行给链路，由 validator/回退兜底。
        # hard_only=True 时跳过本层（仅做硬拦截），用于澄清回复等已有上下文的输入，
        # 避免「是的」这类确认词被当成越界问题拒绝、打断多轮澄清闭环。
        if self.enable_out_of_scope and not hard_only:
            if _OFFTOPIC_HINTS_RE.search(q):
                return Refusal(
                    RefusalCategory.OUT_OF_SCOPE,
                    "命中越界话题词",
                    "我是计量检测业务数据问答助手，只能回答与经营、实验室、设备、"
                    "检测报告等数据相关的问题。例如：「华东区上个月的检测服务收入是多少」。",
                )
            if not self._has_data_signal(q) and not self._is_benign_exempt(q):
                return Refusal(
                    RefusalCategory.OUT_OF_SCOPE,
                    "问题无数据域信号且非豁免项，判定为越界/无关",
                    "我是计量检测业务数据问答助手，只能回答与经营、实验室、设备、"
                    "检测报告等数据相关的问题。例如：「华东区上个月的检测服务收入是多少」。",
                )
        return None

    # ---------------- 内部工具 ----------------

    @staticmethod
    def _match(patterns: list[str], text: str) -> Optional[str]:
        for p in patterns:
            if re.search(p, text, re.IGNORECASE):
                return p
        return None

    @staticmethod
    def _has_data_signal(text: str) -> bool:
        # 命中数据域白名单词即视为数据问题
        if _DATA_SIGNAL_RE.search(text):
            return True
        return False

    @staticmethod
    def _is_benign_exempt(text: str) -> bool:
        """不拒绝的良性输入：问候 / 确认回复 / 英文 / 纯数字 / SQLi 探测 / 短句多轮追问。"""
        if _CONFIRM_RE.match(text):
            return True
        if _BENIGN_GREETING_RE.search(text):
            return True
        if re.search(r"[a-zA-Z]", text):
            return True
        if _DIGITS_ONLY_RE.match(text) and re.search(r"\d", text):
            return True
        if _BENIGN_SQLI_RE.search(text):
            return True
        if len(text) <= 12 and _FOLLOWUP_RE.search(text):
            return True
        return False
