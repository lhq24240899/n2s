# NL2SQL 生产级自然问数系统

一个**可解释、可回退、可观测**的自然语言转 SQL（Text-to-SQL）引擎。核心思路：

> **标签/关键词检索 → 动态 Schema Linking → LLM 生成 → 静态校验 → 执行预检 → 失败回退**

不依赖向量库，检索层完全可解释；校验层用 `sqlglot` 做 AST 级表/列白名单，把「幻觉字段」在进数据库前就拦下来。

---

## 1. 架构与链路

```
用户问题
   │
   ▼
┌─────────────┐   可解释打分(reasons)   ┌──────────────────┐
│ Retrieval   │ ─────────────────────▶ │  RetrievalHit[]  │
└─────────────┘                        └──────────────────┘
   │                                               │
   ▼                                               ▼
┌─────────────┐   候选表 + 1 跳外键邻居            ┌──────────────────┐
│ SchemaLinker│ ─────────────────────▶           │  allowed_tables  │
└─────────────┘                                  └──────────────────┘
   │                                               │
   ▼                                               ▼
┌─────────────┐   注入 schema/口径/示例/错误反馈   ┌──────────────────┐
│ PromptBuilder│ ───────────────────────────────▶ │  LLMClient       │
└─────────────┘                                  └──────────────────┘
   │                                               │
   ▼                                               ▼
┌─────────────┐   sqlglot AST: 写操作/表/列白名单  ┌──────────────────┐
│ Validator   │ ───────────────────────────────▶ │  非法 → 返回错误   │
└─────────────┘                                  └──────────────────┘
   │ 通过                                         │
   ▼                                              │
┌─────────────┐   EXPLAIN / LIMIT 1（真实库把关）  │
│ DBRunner    │ ──────────────────────────────────┘
└─────────────┘
   │ 通过 → 返回 source=llm
   │ 失败 → 携带错误反馈重试(max_retry)
   │ 重试耗尽 → 无命中走 fallback_generic / 否则 fallback_template
```

**每一步的结果都写入 `PipelineTrace`**，最终通过 `GenerationResult.source` 区分来源，
通过 `trace` 精确定位「是哪一层出的问题」。

---

## 2. 目录结构

```
nl2sql/
├── pyproject.toml / requirements.txt / .env.example
├── streamlit_app.py            # ★Web UI 入口（Streamlit Cloud 的 Main file）
├── TESTCASES.md                # ★功能测试用例（含实测标准答案 + 执行记录表）
├── .streamlit/secrets.toml.example  # Streamlit secrets 模板（本地复制为 secrets.toml）
├── nl2sql/                     # 核心包
│   ├── config.py               # pydantic-settings 配置 + 结构化日志
│   ├── models.py               # 全部数据结构 + PipelineTrace
│   ├── knowledge.py            # SchemaRegistry + SQLExampleStore
│   ├── retrieval.py            # RetrievalService（标签+关键词，可解释）
│   ├── linker.py               # SchemaLinker（候选表 + 1 跳外键）
│   ├── glossary.py             # 业务口径（GMV 等统一定义）
│   ├── semantic.py             # ★语义层：Metric/SynonymMap/KnowledgeGraph/SemanticMapper
│   ├── context.py              # ★多轮对话上下文 QueryContext（维度继承）
│   ├── prompt.py               # PromptBuilder + SYSTEM_PROMPT
│   ├── llm.py                  # LLMClient ABC + OpenAI 兼容真实客户端（缺 key 报错）
│   ├── validation.py           # SQLValidator（sqlglot AST 校验）
│   ├── db.py                   # DBRunner ABC + Psycopg(EXPLAIN)（缺 DSN 报错）
│   └── pipeline.py             # Text2SQLPipeline 主流程编排
├── examples/                   # 演示知识库 + 端到端 demo
│   ├── schema.py               # 通用演示知识库
│   ├── demo.py                 # 通用端到端 demo
│   ├── grg_schema.py           # ★广电计量领域知识（表/指标/同义词/知识图谱/示例）
│   ├── grg_engine.py           # ★广电计量引擎编排（真实 LLM + 真实 DB）
│   ├── grg_demo.py             # ★广电计量端到端 demo（单轮/多轮/澄清）
│   └── setup_dev_db.py         # 在真实库建示例表 + 灌种子数据
└── tests/                      # pytest 单测
```

> 与你最初示例的 7 模块一一对应：`models`↔数据模型、`knowledge`↔知识库、
> `retrieval`↔检索层、`linker`↔Schema Linking、`prompt`+`glossary`↔Prompt 构建、
> `llm`↔LLM 接口、`validation`+`db`↔校验+执行、`pipeline`↔主流程。

---

## 3. 快速开始

```bash
# 1) 安装依赖（openai / psycopg 为真实调用所需，必须安装）
pip install -r requirements.txt        # 或: pip install -e .

# 2) 在 .env 填好 LLM__* 与 DB__DSN（见第 4 节）

# 3) 在目标库建示例表 + 灌种子数据（演示用，生产换成你的真实 schema）
python examples/setup_dev_db.py

# 4) 跑端到端 demo（真实 LLM + 真实数据库）
python examples/grg_demo.py     # 广电计量 4 场景
python examples/demo.py         # 通用 3 场景

# 5) 跑单元测试（使用 tests/doubles 里的离线替身，不花真钱、不碰真库）
pytest -q
```

输出会清晰展示三种最终来源：
- `source=llm`：LLM 生成且通过校验 + 预检
- `source=fallback_template`：LLM 幻觉字段被拦下，回退到最相关示例 SQL
- `source=fallback_generic`：检索无命中且生成失败，走全量 schema 兜底

---

## 4. 配置（.env）

复制 `.env.example` 为 `.env`，**必填** `LLM__API_KEY` 与 `DB__DSN`：

```ini
# 接真实模型：填 API key 即走 OpenAI 兼容端点（DeepSeek/通义/vLLM/Ollama 均可）
LLM__API_KEY=sk-xxx
LLM__BASE_URL=https://api.openai.com/v1
LLM__MODEL=gpt-4o-mini

# 接真实库：填 DSN 即走 Psycopg 的 EXPLAIN 预检（建议只读账户）
DB__DSN=postgresql://user:pass@localhost:5432/analytics

# 检索 / 管道
RETRIEVAL__MIN_SCORE=1.0
PIPELINE__MAX_RETRY=1
```

> ⚠️ `LLM__API_KEY` 与 `DB__DSN` **均为必填**。缺省时 `build_llm` / `build_db` 会直接抛错，
> 而不是静默回退到假数据——避免出现「以为在跑真实模型/库、实际跑的是 mock」的隐蔽问题。
> 测试则通过 `tests/doubles.py` 中的离线替身验证编排逻辑，不依赖外部服务。

---

## 5. 你担心的每个问题，这里怎么解

| 你担心的问题 | 本项目机制 |
|---|---|
| 检索不可解释 | `RetrievalHit.reasons` 打印每条命中原因；落入 `PipelineTrace` |
| 检索错了带偏 LLM | `min_score` 阈值过滤低分；无命中走 `fallback_generic` |
| Schema 太大 | `SchemaLinker` 只抽候选表 + 1 跳外键邻居，最小相关 schema |
| 幻觉字段 | `SQLValidator` 用 sqlglot 做**表白名单 + 列白名单**，AST 级拦截 |
| 生成错了没法发现 | `DBRunner.explain`（生产为 EXPLAIN）做执行预检 |
| 重试后仍失败 | 回退到最相关示例 SQL，`source=fallback_template` |
| 定位故障层 | `source` 字段 + `PipelineTrace`（每步 attempt 都有记录） |

---

## 6. 生产替换清单

1. **`llm.py`**：配 `LLM__API_KEY` 即自动切换真实 OpenAI 兼容模型；温度恒为 0。
2. **`db.py`**：配 `DB__DSN` 即走 `PsycopgRunner.explain`（`EXPLAIN`，只读不执行）。
3. **`validation.py`**：已用 `sqlglot` 做 AST 解析与方言（`dialect` 可配 postgres/mysql/...），
   比正则可靠得多；可按需扩展为「语义校验 + 权限校验」。
4. **`retrieval.py`**：若标签覆盖不足，可叠加 BM25（`rank_bm25`）或向量检索，
   但务必保留 `reasons` 可解释性。
5. **`linker.py`**：可换成用 LLM 做一次实体抽取，但结果必须限制在 schema 白名单内。
6. **知识库**：把你的真实 `TableSchema` / `SQLExample` 填进 `examples/schema.py` 即可。

---

## 7. 设计取舍（面试可讲的要点）

- **为什么不用向量？** 向量召回黑盒、难调试、冷启动难。标签/关键词可解释、零依赖，
  对「指标/维度/意图」这类强结构化查询效果足够好；长尾再叠 BM25/向量。
- **为什么校验放两层？** 静态校验（sqlglot AST）拦语法/写操作/幻觉字段；
  执行预检（EXPLAIN）拦「语法合法但运行期才暴露」的问题（类型不匹配、权限、视图不存在）。
- **为什么回退有两级？** 检索无命中（连参考都没有）→ 通用兜底；有命中但 LLM 屡错
  → 回退到已被口径验证过的示例 SQL，保证「至少有可用答案」而不是空手而归。

---

## 8. 广电计量行业适配（语义层 + 多轮 + 报告助手）

通用引擎解决「自然语言→SQL」，但计量检测行业真正难的是「业务语言→数据语义」。
本项目的差异化价值就在新增的**语义层**（`nl2sql/semantic.py` + `nl2sql/context.py`），
它把广电计量的行业知识显式化，作为 LLM 与业务之间的「翻译层」。

### 8.1 语义层四大件

| 组件 | 作用 | 对应你设计文档 |
|---|---|---|
| `Metric`（绑定口径+SQL提示+可切片维度+别名） | 指标定义必须绑定 SQL 表达式，杜绝 LLM 自猜口径 | §3.1 指标体系（集团/运营/质量三层） |
| `SynonymMap`（同义词+歧义澄清） | "EMC"→电磁兼容检测、"软测"→软件测评；歧义词要求澄清 | §3.3 同义词与业务口径库 |
| `KnowledgeGraph`（实体关系） | 实验室-区域-业务线-标准等关系，增强 Schema Linking | §3.2 业务知识图谱 |
| `SemanticMapper.map()` | 把问题确定性映射为「归一化问题+指标+实体」 | §4.1 意图识别+实体抽取+语义层映射 |

实例化见 `examples/grg_schema.py`：9 张 LIMS 风格核心表、5 个广电计量指标
（检测服务收入/检测准时率/报告出具周期/设备利用率/检测一次通过率）、同义词库、知识图谱、带标签示例库。

### 8.2 六阶段管道（在原引擎上叠加语义映射）

```
用户问题
   │
   ▼
┌──────────────┐  同义词展开+指标解析+区域/时间抽取  ┌──────────────────────┐
│ SemanticMapper│ ───────────────────────────────▶ │ MappedQuery(归一化问题) │
└──────────────┘                                  └──────────────────────┘
   │ 歧义? ──▶ 直接返回澄清（不进入生成）
   ▼ 否则
┌──────────────┐  继承上一轮缺失维度（追问"那华南区呢"） ┌──────────────────┐
│ QueryContext │ ───────────────────────────────────▶ │ 补全后 MappedQuery │
└──────────────┘                                      └──────────────────┘
   │
   ▼  （以下复用原引擎：检索→Linking→生成→校验→预检→回退）
Text2SQLPipeline.run(归一化问题, glossary=解析指标口径)
   │
   ▼
返回 SQL + 结果 + 口径说明（可审计："指标=口径；数据来源=表"）
```

### 8.3 多轮对话

`QueryContext` 保存最近一次成功查询的 `metric/business_line/region/time`。
追问只说"华南区"时，自动继承 `{业务线=可靠性, 指标=准时率, 时间=上个月}`，
仅替换区域——这正是你 §4.3 要求的「继承上下文、只替换变化维度」。

### 8.4 跑广电计量 demo

```bash
python examples/grg_demo.py
```

演示覆盖：
1. **单轮语义映射**：`华东区上个月可靠性试验的准时完成率` → 同义词展开、指标解析、注入口径、生成 SQL、口径说明。
2. **多轮继承**：`那华南区呢？` → 继承业务线/指标/时间，仅换区域，返回不同值（华东 0.9167 vs 华南 0.8889）。
3. **歧义澄清**：`那个做环境的实验室利用率怎么样` → 触发歧义同义词，返回澄清问题，不进入生成。
4. **跨业务线**：`集成电路测试的检测一次通过率` → 同义词"集成电路"→ic，跨表关联 test_records。

### 8.5 测试

```bash
pytest -q            # 共 24 个用例：检索/校验/linker/pipeline + 新增 semantic/context/grg
```

新增测试：`tests/test_semantic.py`（同义词/歧义/指标解析）、`tests/test_context.py`（多轮继承）、
`tests/test_grg.py`（端到端单轮/多轮/澄清）。

### 8.6 落地路线（对应你 §六）

| 阶段 | 本项目已提供的能力 | 待补 |
|---|---|---|
| 一、语义层建设 | `Metric`/`SynonymMap`/`KnowledgeGraph` 数据模型 + `examples/grg_schema.py` 示范 | 与财务/运营/质量对齐真实口径、准备 100-200 条评估集 |
| 二、问数 MVP | 完整六阶段管道（含语义映射+多轮+澄清+回退+全链路 trace） | 接真实 LLM/数仓、覆盖 50-100 高频场景 |
| 三、报告助手 | 语义层与查询引擎已可复用；`MappedQuery` + 口径说明天然支撑「确定性模板生成描述」 | 定时触发/多步编排/报告模板(YAML)/推送企业微信 |
| 四、扩展优化 | 检索层已留 `reasons` 可解释接口，便于叠 BM25/向量 | 反馈闭环、NL→PPL/DSL 对接数字化实验室管控平台 |

**关键判断落地**：广电计量已有数据中台/数仓，本项目数据层直接对接 `PsycopgRunner`（只读账户 +
EXPLAIN 预检 + 审计日志），重点投入在语义层而非重复建设数据接入——与你文档结论一致。
演示用的确定性替身（`MockLLM`/`MockDBRunner`/`GRGMockLLM`/`GRGSampleDB`）只保留在 `tests/doubles.py`
供离线单测使用，生产代码已强制走真实 LLM 与真实数据库。

---

## 9. 部署到 Streamlit

Web 入口是仓库根目录的 **`streamlit_app.py`**（部署时的 **Main file**）。

### 9.1 本地运行

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # 填入真实 LLM/DB 凭据
streamlit run streamlit_app.py
```

本地也可继续用 `.env`（`streamlit_app.py` 会优先读 `st.secrets`，两者键名一致）。

### 9.2 Streamlit Community Cloud

1. 打开 https://share.streamlit.io → **New app** → 选仓库 `lhq24240899/n2s`、分支 `main`。
2. **Main file path** 填：`streamlit_app.py`
3. **Advanced settings → Secrets**，粘贴（键名同 `.env`，用 TOML）：

   ```toml
   LLM__BASE_URL = "https://api.ephone.ai/v1"
   LLM__API_KEY  = "sk-..."
   LLM__MODEL    = "gpt-4o-mini"
   DB__DSN       = "postgresql://user:pass@host/db?sslmode=require"
   ```

4. Deploy。依赖由仓库根的 `requirements.txt` 自动安装。

> **数据准备**：表与种子数据在**数据库侧**（Neon），不在 Cloud 上。本地跑一次
> `python examples/setup_dev_db.py` 建好表即可，云端 App 直接复用同一库。

### 9.3 架构说明（为什么这样接）

- Cloud 上**没有 `.env`**，密钥走 `st.secrets`；`streamlit_app.py` 启动时把 secrets 展平后写入
  环境变量（`LLM__BASE_URL` 等），`pydantic-settings` 便能像读 `.env` 一样读到——**核心引擎零改动**。
- 每个浏览器会话**独立持有一个 `GRGQueryEngine`**（含独立 `QueryContext`），所以多轮上下文
  互不串台；侧边栏「清空对话」即 `reset_context()`。
- 结果区展示：数值卡片/表格 + **口径说明**（指标=口径，数据来源=表）+ 可展开的**生成 SQL** 与
  **语义映射 reasons**——把「可解释、可审计」直接暴露给使用者。


