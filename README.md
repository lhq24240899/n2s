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
│   ├── embedding.py            # ★向量化：OpenAI 兼容 + 本地 HashingEmbedder（离线可测）
│   ├── vectorstore.py          # ★pgvector + pg_trgm 存储（文档库 + 示例库向量索引）
│   ├── fusion.py               # ★RRF 倒数排名融合（与存储/模型无关，可复用）
│   ├── kb.py                   # ★混合检索：三路召回 + RRF + 精排 + 带引用文档问答
│   ├── rerank.py               # ★精排层（LLM 打分 + 低分截断，可插拔换 cross-encoder）
│   ├── graph.py                # ★LangGraph 版编排（六层节点化 + Critic 反思节点）
│   └── pipeline.py             # Text2SQLPipeline 手写编排（与 graph.py 对照）
├── examples/                   # 演示知识库 + 端到端 demo
│   ├── schema.py               # 通用演示知识库
│   ├── demo.py                 # 通用端到端 demo
│   ├── grg_schema.py           # ★计量检测领域知识（表/指标/同义词/知识图谱/示例）
│   ├── grg_engine.py           # ★计量检测引擎编排（真实 LLM + 真实 DB + 混合 RAG 路由）
│   ├── grg_demo.py             # ★计量检测端到端 demo（单轮/多轮/澄清）
│   ├── kb_docs.py              # ★企业知识库语料（19 篇：指标口径/术语/标准/排错 FAQ）
│   ├── setup_kb.py             # ★建知识库：建表 + 向量化 + 写入 + 检索自检
│   ├── graph_demo.py           # ★手写 pipeline vs LangGraph 对照演示
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
python examples/grg_demo.py     # 计量检测 4 场景
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
| SQL 正确但匹配 0 行 / 全 NULL | **空结果自愈**：`query()` 检测到空结果，带「过滤条件可能不匹配」反馈重生成一次（`retry_on_empty`，可关）；UI 自动展开 SQL |
| 问「各业务线/各实验室」却只返回一个总数 | 语义层识别**分组维度**（`各/按/每个 + 维度词`）→ Glossary 下发 `GROUP BY` 指令（每行一个取值），且**不再把同维度继承成过滤条件** |
| 会话中途答错一次后，后面全查不到数据 | 连接用 **autocommit**（每语句独立事务）避免坏语句毒化整条连接，并**异常即弃连接重连**——否则 PostgreSQL 的 `current transaction is aborted` 会让整条连接持续失败 |
| 文档类问题（"EMC 是什么""为什么华南区查不到数据"） | 走**企业知识库混合 RAG**：关键词 + pg_trgm + pgvector 三路召回 → RRF 融合 → **只依据资料作答并标注引用**，避免 RAG 变成新的幻觉源 |
| 重试后仍失败 | 回退到最相关示例 SQL，`source=fallback_template` |
| 定位故障层 | `source` 字段 + `PipelineTrace`（每步 attempt 都有记录） |

---

## 6. 生产替换清单

1. **`llm.py`**：配 `LLM__API_KEY` 即自动切换真实 OpenAI 兼容模型；温度恒为 0。
2. **`db.py`**：配 `DB__DSN` 即走 `PsycopgRunner.explain`（`EXPLAIN`，只读不执行）；
   连接为 **autocommit**（只读场景，避免一条坏语句让整条连接持续报
   `current transaction is aborted`），且任何异常都会丢弃连接、下次自动重连。
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

## 8. 计量检测行业适配（语义层 + 多轮 + 报告助手）

通用引擎解决「自然语言→SQL」，但计量检测行业真正难的是「业务语言→数据语义」。
本项目的差异化价值就在新增的**语义层**（`nl2sql/semantic.py` + `nl2sql/context.py`），
它把计量检测行业的业务知识显式化，作为 LLM 与业务之间的「翻译层」。

### 8.1 语义层四大件

| 组件 | 作用 | 对应你设计文档 |
|---|---|---|
| `Metric`（绑定口径+SQL提示+可切片维度+别名） | 指标定义必须绑定 SQL 表达式，杜绝 LLM 自猜口径 | §3.1 指标体系（集团/运营/质量三层） |
| `SynonymMap`（同义词+歧义澄清） | "EMC"→电磁兼容检测、"软测"→软件测评；歧义词要求澄清 | §3.3 同义词与业务口径库 |
| `KnowledgeGraph`（实体关系） | 实验室-区域-业务线-标准等关系，增强 Schema Linking | §3.2 业务知识图谱 |
| `SemanticMapper.map()` | 把问题确定性映射为「归一化问题+指标+实体」 | §4.1 意图识别+实体抽取+语义层映射 |

实例化见 `examples/grg_schema.py`：9 张 LIMS 风格核心表、5 个计量检测指标
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

**澄清闭环（易被忽略的一点）**：遇到歧义时不能只回一句"请澄清"就结束。若那样，
用户回"是的"会被当成一个**全新问题**——原问题的指标丢了，还会错误继承上一轮的旧指标
（实测出现过：问"那个做环境的实验室利用率怎么样"，确认后却答成了"检测服务收入"）。
`GRGQueryEngine` 的做法是把「原问题 + 歧义同义词 + 消歧后的取值」存为**待澄清态 `pending`**，
用户确认后用规范词**重写原问题再跑一遍**；同时只把「明确确认」或「短且自身无指标的补充答复」
当作澄清回应，带指标的新问题仍按新问题处理（不折叠、不污染上下文）。

**分组维度（GROUP BY）**：「各业务线 / 各实验室 / 各区域的 X」问的是**分布**，不是单个总数。
语义层用 `各/按/每个 + 维度词` 识别出 `entities["group_by"]`，并在 Glossary 里下发硬指令
（必须 `GROUP BY` 对应维度、**每个取值一行**、不要对该维度再加过滤）；同时
`QueryContext.inherit()` 会**拒绝把「本轮要分组的维度」继承成过滤条件**——否则上一轮问过
"华东区可靠性…"之后，再问"各业务线的准时率"就会被悄悄过滤成只剩一条线、只返回一个数值。

### 8.4 跑计量检测 demo

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

**关键判断落地**：该集团已有数据中台/数仓，本项目数据层直接对接 `PsycopgRunner`（只读账户 +
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

- **机构名称可配置**：页面/标题上的机构名统一由 `streamlit_app.py` 顶部的 `DISPLAY_NAME`
  常量控制，想换名只改那一行（不涉及任何业务逻辑）。
- Cloud 上**没有 `.env`**，密钥走 `st.secrets`；`streamlit_app.py` 启动时把 secrets 展平后写入
  环境变量（`LLM__BASE_URL` 等），`pydantic-settings` 便能像读 `.env` 一样读到——**核心引擎零改动**。
- 每个浏览器会话**独立持有一个 `GRGQueryEngine`**（含独立 `QueryContext`），所以多轮上下文
  互不串台；侧边栏「清空对话」即 `reset_context()`。
- 结果区展示：数值卡片/表格 + **口径说明**（指标=口径，数据来源=表）+ 可展开的**生成 SQL** 与
  **语义映射 reasons**——把「可解释、可审计」直接暴露给使用者。

---

## 10. 企业知识库与混合 RAG

结构化问数解决不了「EMC 是什么」「为什么问华南区查不到数据」这类**文档性问题**，
而企业里这类知识（指标口径、业务术语、检测标准、排错经验）恰恰最容易让模型瞎编。
本项目的做法是：**同一个入口，两条分支，各用各的长处，还能互相补强**。

```
用户问题
   │
   ▼
┌──────────────────────────┐  三路召回（同一个 pgvector 表）
│ 混合检索 HybridDocRetriever│ ──┬─ 关键词命中（精确子串，最可解释）
└──────────────────────────┘   ├─ pg_trgm word_similarity（容错：错别字/语序）
   │                           └─ pgvector 余弦相似度（语义改写）
   │                                  └──▶ RRF 倒数排名融合 → top-k
   ▼
┌───────────────┐  解析出指标 / 要求分组 ?
│  路由 Route    │── 是 ──▶ 结构化问数：SQL 生成时**注入知识库摘录**作口径补充
└───────────────┘                    （补上表结构看不出来的口径与已知坑）
   │ 否
   └──────────────▶ 文档问答：**只依据检索到的资料作答**，并标注引用 [1][2]
```

### 10.1 为什么用 pgvector（而不是另引一套向量库）

- **一个库搞定**：结构化数据（表/指标）与文档（口径/标准/术语）都在同一个 Neon 库，
  天然支持「向量检索 ⊕ 结构化查询」的混合检索，不用维护两套数据的一致性；
- **Neon 免运维**，Streamlit Cloud 上零额外服务；
- **可解释性不丢失**：向量分数与标签/关键词分数一起暴露在 `reasons` 里，
  不是黑盒召回。

### 10.2 三个工程细节（都踩过坑）

1. **中文短查询必须用 `word_similarity`，不能用 `similarity`**：
   后者是「整串 vs 整串」，短查询对长文档几乎必然低于默认阈值 0.3，会一路召回为空。
   `word_similarity(query, doc)` 取「查询三元组 vs 文档任意片段」的最大相似度才对。
2. **RRF 融合只依赖名次**，三路信号量纲不同也无需归一化/调权重，鲁棒且好讲。
3. **`jsonb` 字段要传 JSON 文本**：psycopg 会把 Python `list` 适配成 PG 数组字面量
   （`{指标,口径}`），写 jsonb 会报 `invalid input syntax for type json`。

### 10.3 建库与自检

```bash
python examples/setup_kb.py                 # 建文档库 + 示例向量索引（幂等）
python examples/setup_kb.py --demo          # 跑一组混合检索自检（打印三路命中 + 精排分）
python examples/setup_kb.py --query "为什么问华南区查不到数据"
```

知识库语料在 `examples/kb_docs.py`（19 篇：5 个指标口径 + 业务线/术语 + 检测标准 + 排错 FAQ），
**每条都与 `grg_schema.py` 的指标口径严格对齐**——否则文档与数据打架，RAG 会变成新的幻觉来源。

### 10.4 两阶段检索：召回 ⊃ 精排

```
标签分 ⊕ 示例向量  →  RRF  →  LLM 精排  →  低分截断  →  注入 prompt
   （召回，求不漏）              （精排，求排序准 + 去噪）
```

- **召回阶段**同时覆盖两类语料：知识文档（关键词 / trgm / 向量三路）与 **SQL 示例库**
  （标签分 ⊕ 示例问题向量，同样 RRF 融合）。长尾问句（"上个月华东片区可靠性这块的
  验收及时比例是多少"）标签完全命不中，靠示例向量召回可以兜住。
- **精排阶段**用 LLM 对候选打 0~10 分并**丢弃低于阈值的候选**。实测一句"为什么问华南区
  查不到数据"会召回 8 条候选，精排后只剩 2 条真正相关的——把噪声挡在 prompt 之外，
  是"降低模型幻觉"很实际的一环。
- 精排接口可插拔（`Reranker`）：网关没有专用 rerank 模型时用 `LLMReranker`，
  将来换 cross-encoder / 专用 rerank API 只需新增一个类。

---

## 11. 编排：手写 pipeline vs LangGraph

同一批组件（Retriever / Linker / PromptBuilder / LLM / Validator / DB），**两套编排实现**：

```
START → retrieve → link → generate → validate ─ok→ execute → critique ─ok→ END
                    ▲          │err           │err              │revise
                    └──────────┴──────────────┘                  │
                      还有重试额度则带反馈重生成 → fallback → END ←┘
```

| | `pipeline.py`（手写） | `graph.py`（LangGraph） |
|---|---|---|
| 控制流 | 嵌套 for/if，读一段才懂全貌 | 节点 + 条件边，**流转条件独立可读** |
| 加一步（如 Critic） | 改主流程、易碰坏重试逻辑 | 加一个节点 + 一条边，**局部改动** |
| 断点续跑 / 人工介入 | 需自己实现 | checkpoint + `interrupt` 原生支持 |
| 可视化 | 手画 | `graph.mermaid()` 一键导出 |
| 调试 | 自己的 trace | 每节点状态快照可回放 |

**框架不解决什么**（这部分仍是自己的组件）：口径治理（Glossary / 语义层）、
SQL 安全（sqlglot 白名单 + EXPLAIN 预检）、检索可解释性（reasons）、
多轮上下文与澄清闭环 —— 换编排不会让这些变好。

**Critic 反思节点**在这里承担"自检"职责：先做零成本规则检查（结果为空/全 NULL → 打回），
再按需做 LLM 一致性复核（`critique_llm=True`，默认关以免每次查询都双倍成本）。

```bash
python examples/graph_demo.py               # 两条编排对照跑同一批问题
python examples/graph_demo.py --llm-critic  # 额外开启 LLM 复核
```

### 11.1 降幻觉的三层做法（两套编排共用）

| 层次 | 做法 |
|---|---|
| 生成前 | Schema Linking 限定表列 + 口径 Glossary + 知识库摘录 + 示例向量召回（把"业务真相"喂进去） |
| 生成中 | 温度 0；静态校验（sqlglot AST 白名单）+ 执行预检（EXPLAIN） |
| 生成后 | **Critic 反思**：结果为空/全 NULL → 打回重生成；低分候选截断；RAG 答案强制带引用，资料不足时明确说"资料中未涉及" |



