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
│   ├── auth.py                 # ★鉴权：JWT 签发/校验 + 角色→scope（认证与授权分离）
│   ├── policy.py               # ★数据权限：表/列/指标白名单 + 行级过滤注入（sqlglot）
│   ├── service.py              # ★服务层：会话/限流/并发闸门/审计/序列化（框架无关）
│   ├── api.py                  # ★FastAPI 智能体 API（四道门：认证/授权/数据权限/执行安全）
│   ├── mcp_server.py           # ★MCP server（只读工具，供 IDE / 其它智能体调用）
│   ├── bootstrap.py            # ★装配层（三个入口共用同一套上下文与引擎工厂）
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
│   ├── api_server.py           # ★启动智能体 API（uvicorn）
│   └── setup_dev_db.py         # 在真实库建示例表 + 灌种子数据
└── tests/                      # pytest 单测
    ├── doubles.py              # 离线替身（MockLLM / GRGMockLLM / GRGSampleDB）
    └── service_factory.py      # 共享装配：用替身搭出真实服务栈（API/MCP 测试复用）
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

# 5.5) 跑问数评估集（57 条，真 LLM + 真库，输出 execution accuracy）
python examples/eval_run.py                 # 全量，报告写入 evals/last_report.json
python examples/eval_run.py --category 分组 # 只跑某类

# 6) 起智能体 API / MCP server（第 12 节）
python examples/api_server.py --port 8000      # → http://127.0.0.1:8000/docs
python -m nl2sql.mcp_server                    # stdio，给 IDE / 其它智能体用

# 7) schema 变更检测（对接元数据中心后的 CI 卡口）
python examples/metadata_check.py --save       # 首次保存快照
python examples/metadata_check.py              # 有变更输出明细，退出码 1
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
| 谁能查、能查哪些数据 | **四道门**（第 12 节）：JWT 认证 → scope 授权 → 数据权限（无权表不进 prompt、指标级拒绝、行级过滤注入 SQL）→ 库级只读 |
| 同一条问题不同角色看到的口径不一样 | 令牌携带 `regions / business_lines` → 合成 `DataPolicy` → **过滤条件注入 SQL 的 WHERE**（不是查完再截断，聚合值本身就是对的） |
| 调用方把 LLM 预算刷干 / 下游被打崩 | 按主体的**令牌桶限流**（429 + `Retry-After`）+ **有界并发信号量**（超时即拒绝），即"算力调度"的最小有效形态 |
| 出了事怎么追溯 | **审计**：谁/何时/问什么/生成什么 SQL/返回几行/是否被拒绝，落结构化日志并可从 `/v1/audit` 查 |
| 只读会不会只是"约定" | 不止：`DB__READONLY=true` 时连接后执行 `SET default_transaction_read_only = on`，写入会被**数据库本身**拒绝（实测 `cannot execute INSERT in a read-only transaction`），且连接不被毒化 |
| 重试后仍失败 | 回退到最相关示例 SQL，`source=fallback_template`；**回退路径同样过数据权限**，过不了就置空 SQL |
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
7. **权限与审计**（第 12 节已实现，生产需替换三处）：
   - `auth.py` 的 JWT 换成企业 **IdP / SSO** 签发的令牌（保留 `Principal` 抽象即可，其余不动）；
   - `policy.py` 的 `DEFAULT_POLICIES` 换成读**权限系统/元数据中心**（角色 → 表/列/指标/行级规则）；
   - 数据库侧再叠一层：**只读账户**（`GRANT SELECT`）+ 行级安全（PG RLS）。
     代码里的 `DB__READONLY` 是"会话级只读"，与账户级只读不冲突，属于纵深防御。

---

## 7. 设计取舍（面试可讲的要点）

- **为什么一开始不上向量？** 向量召回黑盒、难调试、冷启动难；标签/关键词可解释、零依赖，
  对「指标/维度/意图」这类强结构化查询效果足够好。**先用可解释方案把上限摸清**，
  确认长尾问句（"上个月华东片区可靠性这块的验收及时比例"）标签命不中之后，
  再叠三路混合召回 + RRF + 精排——且**始终保留 `reasons`**，不让召回变回黑盒。
- **为什么校验放两层？** 静态校验（sqlglot AST）拦语法/写操作/幻觉字段；
  执行预检（EXPLAIN）拦「语法合法但运行期才暴露」的问题（类型不匹配、权限、视图不存在）。
- **为什么权限要放四层（认证/授权/数据权限/库级只读）？** 单一防线必然有盲区：
  AST 白名单管不到"语义合法但越权"的查询，行级过滤在应用层、总有绕过的可能。
  每层只解决一类问题、互不重复，且**最后一层落在数据库**——应用层全被绕过也写不进数据。
- **为什么回退有两级？** 检索无命中（连参考都没有）→ 通用兜底；有命中但 LLM 屡错
  → 回退到已被口径验证过的示例 SQL，保证「至少有可用答案」而不是空手而归。
  但**回退路径同样过数据权限**：宁可不返回数据，也不返回越权 SQL。

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

### 8.5 测试与评估

```bash
pytest -q                        # 172 个离线用例：检索/校验/linker/semantic/context/grg
                                 #   + kb(混合RAG)/rerank/graph(图编排)/auth/policy/api/mcp/metadata
                                 #   + dsl(IR->ES DSL/PPL)/es_engine(MockTransport 端到端)
python examples/eval_run.py      # 57 条在线评估（真 LLM + 真库）→ execution accuracy
python examples/es_eval.py       # 9 条在线评估（真 ES 集群）→ 第二执行引擎 execution accuracy
```

测试分层：`tests/doubles.py` 提供确定性替身（不联网、不花钱、不碰真库），
`tests/service_factory.py` 用替身搭出**真实服务栈**（真实 QueryService + 引擎 + 语义层 + 权限层），
因此 `test_api.py` / `test_mcp.py` 覆盖的是真实调用链，而不是 mock 出来的假接口。

**评估与测试的分工**：pytest 验证"代码逻辑对不对"（离线、秒级）；
评估集验证"**答得准不准**"（在线、按 execution accuracy 计分）。
评估集的标准答案是一段段**标准 SQL**——与系统生成的 SQL 在同一库上各跑一遍、按值比对，
所以种子数据变了评估集依然有效。详见第 13 节。

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

### 9.1.1 三档执行引擎切换（SQL / DSL / PPL）

侧边栏「🔀 执行引擎」可以在**同一个问题**上切换真跑的引擎——这是「自然语言转 SQL/PPL/DSL」
里 DSL/PPL 那一半的可交互落点：

| 档位 | 真实落点 | 说明 |
|---|---|---|
| 🔢 SQL | PostgreSQL（`Text2SQLPipeline`） | 语义层 + 混合 RAG + sqlglot 校验 + EXPLAIN 预检 |
| 🔎 DSL | Elasticsearch `_search` | 问题 → 确定性规则填槽 → `QueryIR` → 编译 DSL → 真执行 |
| 🧭 PPL | OpenSearch `_plugins/_ppl` | 同一份 `QueryIR` 编译成 PPL → 真执行（OpenSearch 专有） |

三档**共用同一套 IR/语义层与安全护栏**——换引擎不换安全等级（ES 路径同样先过 `SafetyGuard` 硬拦截）。
页面会把**实际下发的 DSL 与 PPL 两条语句**都列出来对照，方便看清楚"同一份中间表示如何编译成两种方言"。

侧边栏「🔌 数据源自检」也**随档位切换**（`datasource_selfcheck(mode)`）：SQL 档给库/方言 + 核心表行数，
DSL/PPL 档给集群版本 + 索引文档数 + level/region 分布；PPL 档再多一条「PPL 真执行」——
它真跑一条 `stats count()`，用来区分"OpenSearch 端点可用"和"回退到普通 ES 的仅编译降级"。

配置（`.env` 或 Streamlit Secrets，键名一致）：

```bash
ES__ENABLED=true
ES__HOST=http://es-cn-xxx.public.elasticsearch.aliyuncs.com:9200   # 阿里云公网入口是 http 明文
ES__USER=elastic
ES__PASSWORD=...
ES__INDEX=device_events

# 可选：PPL 专用 OpenSearch 端点（不配则 PPL 回退 ES__HOST 并降级为「仅编译」）
ES__PPL_ENABLED=true
ES__PPL_HOST=https://<project>-<svc>.b.aivencloud.com:26380
ES__PPL_USER=avnadmin
ES__PPL_PASSWORD=...
```

灌数据与验证：

```bash
python examples/setup_es_demo.py --target both   # 设备日志 1222 条，两个集群都灌（按构造可复现）
python examples/es_eval.py                       # 双份成绩单：ES DSL x/y + PPL m/n
```

**编译层消化方言差异（含字符集）**：这份 IR 已经吸收了三种真实的方言差异——
① 时间：ES 的 `now-7d` date math 在 PPL 里换算成绝对时间；
② 字符集：PPL 引擎（Calcite）按 ISO-8859-1 编码字面量，**任何中文字面量都会 500**
（`Failed to encode '华东' in character set 'ISO-8859-1'`，换 `U&'..'`/`like`/`match`/双引号都绕不过）。
因此索引里为需要过滤的中文字段准备了 ASCII 伴生字段（`region_code`/`lab_code`/`bl_code`/`msg_code`，见
`examples/setup_es_demo.py` 的 `MAPPING`），PPL 编译时自动改写：`region = '华东'` → `region_code = 'east'`。
于是**同一份 IR 编译出两种方言，DSL 用中文原文、PPL 用编码字段**，两条链路都真执行、都 100% 通过；
页面上会把做过的适配逐条列出来，避免"PPL 语句里的字段和问题对不上"的困惑。
DSL 档不受影响（ES 的 JSON 走 UTF-8）。

**多轮上下文（三档都有）**：DSL/PPL 档也支持追问 —— 与 SQL 路径的 `QueryContext` 同构
（`examples/es_engine.py::EsContext`）：本轮显式说的维度覆盖上一轮、没说但上轮说过的补齐、
**本轮要分组的维度不作为过滤条件继承**（否则「各区域…」会只剩一行）、行级权限每轮重新施加不参与继承。
与 SQL 侧的差别只有一处：ES 侧**只在「追问」时继承**（`is_follow_up`：那/呢/还有/换成…或极短维度词），
因为 ES 问句短且自成一体，无条件继承会让用户随手点对话区示例问题时莫名带上上一轮的区域。
真机实测：`华东区最近7天的ERROR告警有多少条`(42) → `那华南区呢？`(30，只换区域) →
`那最近30天呢？`(42，只换时间) → `各区域最近7天的ERROR告警数量`(3 行，不继承)。

**空结果回退（继承维度把结果筛空时）**：多轮继承有个反直觉的副作用 ——
先问「华东区上个月可靠性试验的准时完成率是多少」，再问「EMC 检测的准时率是多少」，
后半句没说区域/时间，于是继承了 `区域=华东、时间=上个月`；可 **EMC 的报告全在北京（华北）**，
被继承来的华东一过滤就是空，页面显示"无匹配数据"，用户以为系统算错了。
对策（`GRGQueryEngine._answer`）：结果为**空/全 NULL** 且本轮带入了**继承维度**时，
忽略这些继承维度重查一次 —— 只在"继承来的"维度上放宽，**用户本轮明说的条件绝不动**
（显式问「华东区 EMC 的准时率」时即使查不到也不会被擅自放宽），界面上用醒目提示说明
"已忽略上一轮继承的『区域、时间』"，并列出本轮真实生效的过滤条件。
真机实测：上述两轮从 `NULL` 变成 **0.75**（全区域 EMC 口径），而「那华南区呢」这类正常追问不受影响。

⚠️ 实现要点（第一版就踩了）：重查时**必须重建 Glossary**。口径注入会把实体渲染成
「必须使用 labs.region = '华东'」这种**硬约束**塞进提示词，不重建的话即使问题文本已放宽，
LLM 仍会照抄旧口径把区域过滤写回 SQL —— 重查照样为空，回退**静默失效**。
（对应回归测试 `tests/test_grg.py::test_empty_result_relaxes_inherited_filters`，
其替身专门模拟"照抄口径约束"的真实 LLM 行为。）

注意：越界软拦截不套用（其词表按计量检测业务建，
套到事件日志域会误杀），但密钥提取/提示词注入/PII 的**硬拦截**照常生效。

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

**编排可替换，引擎零改动**：`graph.GraphRunner` 让图暴露与 `pipeline` 相同的
`query()` 契约（外加 `glossary / doc_context / guard` 三个可变属性），
因此引擎只换 `runner` 就能在两种编排间切换——这也顺带证明了"换编排不换安全等级"：
数据权限守卫在两条路径上都生效（有单测守着）。

```bash
python examples/graph_demo.py               # 两条编排对照跑同一批问题
python examples/graph_demo.py --llm-critic  # 额外开启 LLM 复核
python examples/api_server.py --orchestrator graph   # API 也可切到图编排
```

### 11.1 降幻觉的三层做法（两套编排共用）

| 层次 | 做法 |
|---|---|
| 生成前 | Schema Linking 限定表列 + 口径 Glossary + 知识库摘录 + 示例向量召回（把"业务真相"喂进去） |
| 生成中 | 温度 0；静态校验（sqlglot AST 白名单）+ 执行预检（EXPLAIN） |
| 生成后 | **Critic 反思**：结果为空/全 NULL → 打回重生成；低分候选截断；RAG 答案强制带引用，资料不足时明确说"资料中未涉及" |

---

## 12. 智能体 API：FastAPI + MCP + 只读数据权限

三个入口，一套内核：`bootstrap.py` 装配一次，`api.py`（HTTP）、`mcp_server.py`（MCP）、
`streamlit_app.py`（Web UI）共用同一个 `QueryService` 与引擎工厂
——避免"三个入口三套行为"这种最常见的腐化。

### 12.1 四道门（职责不重叠、也不留缺口）

```
请求
 ├─① 认证  Bearer JWT           你是谁 → 401 / 503（未配密钥）
 ├─② 授权  scope 声明式校验      能做什么动作 → 403
 ├─③ 数据权限  DataPolicy        能看哪些数据 → 生成前收窄、生成后改写 SQL
 └─④ 执行安全  AST 白名单 + EXPLAIN + 库级只读   能执行什么 → 拒绝并回退
```

- **① 认证**：HS256 JWT，固定算法（防 `alg=none` 降级）、校验 `exp/iss/aud`。
  **权限一律以服务端角色映射为准**——即便令牌里被塞了越权 scope，也不会跟着越权。
- **② 授权**：接口只声明"需要哪个 scope"（如 `Depends(require(Scope.QUERY_ASK))`），
  不写 `if role == "admin"`；角色 → scope 的映射只在 `auth.py` 一处维护。
- **③ 数据权限**（`policy.py`）落在三处：
  1. **生成前摘掉无权表**：LLM 看不到的表就写不出来，比事后拦截干净；
  2. **指标级拒绝**：明确回"你的角色无权查询该指标"，而不是给一条被拦的 SQL；
  3. **生成后列级拦截 + 行级过滤注入**：用 sqlglot 找到**真正引用该表的那层 SELECT**，
     把 `region = '华东'` 之类谓词 **AND 进 WHERE**（而非查完再在 Python 里过滤——
     那样行数/聚合值已经算错了）。SQL 未引用该表时**如实标注"未生效"**，绝不假装过滤成功。
- **④ 执行安全**：AST 白名单 + EXPLAIN 预检 + **`SET default_transaction_read_only = on`**。
  前三道都在应用层；最后一道在数据库层——即使应用层被绕过，`INSERT/DROP/UPDATE`
  也会被 PG 直接拒绝（实测：`cannot execute INSERT in a read-only transaction`）。

> 回退路径同样过权限。实测踩到过：重试耗尽后回退到示例模板 SQL，而模板带着被禁字段，
> **越权数据被直接返回**。现在 `pipeline.py` 与 `graph.py` 的 fallback 都必须过 `guard.post_sql`，
> 过不了就把 `sql` 置空（宁可不返回数据）。这条有单测守着。

### 12.2 服务层（`service.py`，刻意不依赖 FastAPI）

| 能力 | 做法 | 为什么必须有 |
|---|---|---|
| 会话 | 每会话一个引擎（独立多轮上下文）+ **TTL 过期 + 容量上限（LRU）** | 不做这两件事的 Agent 服务，跑一夜就 OOM |
| 会话归属 | `owner` 校验，换个 `session_id` 不能接管别人的上下文 | 否则等于越权读别人的历史问题与口径 |
| 限流 | 按 `sub` 的令牌桶，超限 429 + `Retry-After` | 防某个调用方把 LLM 预算刷干 |
| 并发闸门 | 有界信号量（`API__MAX_CONCURRENCY`），排队超时即拒绝 | 这就是"算力调度"最有效的形态：保护下游而不是让它雪崩 |
| 审计 | 谁/何时/问什么/生成什么 SQL/返回几行/是否被拒绝，落结构化日志 + 内存环形缓冲 | 只读系统同样要审计：泄露常发生在"合法查询"里 |

**并发模型（容易被忽略的细节）**：引擎内部是同步阻塞的（openai SDK、psycopg 都是同步客户端），
所以路由写成普通 `def`，让 FastAPI 丢进线程池，再用信号量限制真正打到 LLM 的并发。
若写成 `async def` 直接调同步代码，会**阻塞事件循环**，把整台服务拖死。

### 12.3 接口一览

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/health` | — | 探活：DB/KB/会话数/并发占用 |
| POST | `/v1/auth/token` | — | 自助签发令牌（**仅本地演示**，需 `AUTH__DEV_TOKEN_ENDPOINT=true`） |
| GET | `/v1/whoami` | 登录 | 当前身份、角色、scope、数据范围 |
| GET | `/v1/schema` | `schema:read` | **按角色过滤**的表/指标目录（敏感字段会被标记） |
| POST | `/v1/kb/search` | `kb:read` | 知识库混合检索（带召回依据） |
| POST | `/v1/ask` | `query:ask` | 问数：`result / rag / clarification / denied` 四种结果 |
| POST | `/v1/sessions/{id}/reset` | `query:ask` | 重置会话上下文 |
| GET | `/v1/audit` | `audit:read` | 近期审计记录（仅 admin） |

### 12.4 跑起来

```bash
# 1) 签发令牌（本地演示；生产应接企业 IdP）
python -m nl2sql.auth --role analyst --sub alice
python -m nl2sql.auth --role analyst --sub east-mgr --regions 华东    # 带数据范围的令牌

# 2) 起 HTTP 服务（文档：http://127.0.0.1:8000/docs）
python examples/api_server.py --port 8000
# 或： uvicorn "nl2sql.api:create_app" --factory --port 8000

# 3) 起 MCP server（stdio，给 IDE / 其它智能体用）
python -m nl2sql.mcp_server                    # 默认 analyst（只读）
python -m nl2sql.mcp_server --transport sse --port 8765
```

实测（真 LLM + 真 Neon 库，uvicorn + curl）：

```jsonc
// POST /v1/ask {"question": "各实验室设备利用率"}   ← 令牌限定 regions=["华东"]
{
  "type": "result", "row_count": 3,
  "rows": [["上海集成电路实验室", 0.88], ["无锡可靠性实验室", 0.83], ["广州计量实验室", 0.81]],
  "sql": "SELECT l.name, AVG(e.utilization) AS utilization FROM equipment AS e
          JOIN labs AS l ON e.lab_id = l.id
          WHERE l.region = '华东' GROUP BY l.name ORDER BY utilization DESC",
  "data_scope": ["已注入行级过滤: labs.region IN ['华东']"]
}
```

同一个问题，不带数据范围的令牌会返回全部 5 个实验室——**权限差异体现在 SQL 上**，
而不是查完再截断，所以聚合值本身也是正确的。

### 12.5 MCP：给智能体用，不是给前端用

HTTP API 面向前端；MCP 面向**宿主模型**——把能力以「工具 + 说明」注册出去，模型自己决定何时调用。

| 工具 | 用途 |
|---|---|
| `ask_business_question(question, session_id)` | 自然语言问数（支持多轮、分组、口径问答） |
| `list_metrics()` / `list_tables()` / `get_table_schema(table)` | 先看可用指标与字段，再提问，命中率更高 |
| `search_knowledge(query, top_k)` | 知识库检索（返回召回依据，可解释） |
| `reset_session(session_id)` | 换话题时清空上下文 |

安全性：**根本没有注册任何写操作工具**；角色由启动参数/环境变量固定；
数据权限与 HTTP 入口完全一致（行级过滤同样注入 SQL）。




---

## 13. 评估体系与元数据接入

这一节对应 JD 第 2 条里最容易被忽略的两句：「搭建智能问数**全流程**能力」（全流程必须**可衡量**）
和「**对接**企业数据系统」（表结构不能永远手写在代码里）。

### 13.1 评估集：57 条 + 自动跑分（execution accuracy）

```bash
python examples/eval_run.py                  # 全量，报告写 evals/last_report.json
python examples/eval_run.py --category 分组  # 只跑某类
python examples/eval_run.py --min-accuracy 0.9   # 低于阈值退出码 1，可接 CI
```

设计（`evals/harness.py` + `evals/cases.py`）：

- **标准答案不是硬编码数字，而是标准 SQL**——与系统生成的 SQL 在同一个库上各跑一遍、按值比对。
  这是 NL2SQL 领域标准的 evaluation 做法：种子数据变了评估集依然有效，还顺带验证标准 SQL 本身。
- 浮点按 4 位小数归一；多行结果按**排序后的集合**比对（GROUP BY 行序不影响对错）；
  「最高的实验室是哪个」允许系统额外返回利用率列，只要标准值出现在任一列即算命中。
- 57 条覆盖 8 类：指标值（准时率/收入/利用率/一次通过率/周期）、分组、排名 TopN、总量计数、
  **多轮继承**、澄清、文档 RAG（必须带引用）、**安全**（写请求/注入必须被拒）、边界健壮。
- 比对逻辑是纯函数，`tests/test_eval.py` 离线单测（不起服务也能测 harness）。

**首轮基线 75.4% → 修复后 93.0%**，每一处提升都是评估集抓出来的真实缺陷：

| 轮次 | 通过率 | 评估集抓到的缺陷 → 修复 |
|---|---|---|
| R1 | 75.4% | 「总量」问句 0/8——"多少台设备"没有注册指标，**被错误路由进文档问答** → 语义层新增 `count` 意图 + Glossary 下发 `COUNT(*)` 指令；"上个月"时而滚动窗口时而自然月 → 时间口径钉死进 Glossary |
| R2 | 91.2% | TopN 问句（"报告数量最多的业务线"）仍走 RAG → 补 `topn` 意图进路由；LLM 只返回聚合数值不返回"是哪个" → 排名约束要求第一列是名称列 |
| R3 | **93.0%** | 剩余 4 条失败各有明确根因，不再盲修 |
| R4 | **96.9%** | 灌入经营数据后扩到 65 条；新用例首测 6/8，抓出 3 个指标口径问题并治理（详见 §15.2） |

**当前（65 条口径）剩余 2 个已知失败（诚实记录）**：
- `每个客户的合同总金额`：LLM 用 **id 列**而不是名称列分组，且**自行添加了问句未提及的过滤条件**
  （NL2SQL 的经典病；Glossary 已明确要求名称列，属模型服从性的边界 case）；
- `设备利用率最低的三个实验室`：系统 SQL 多 JOIN 了一层业务表，导致聚合值被放大（0.75 vs 标准 0.76）——
  与 C05 同类，都是"模型生成了多余的 JOIN/过滤"。

> 面试价值：**"你的准确率多少？怎么测的？"** —— 65 条 9 类、通过 63 条（96.9%），标准答案是一段段标准 SQL、按执行结果比对，
> 且能说出"从 75% 到 93% 的每一分是修了什么"。这比任何"我做了 NL2SQL"都有说服力。

### 13.2 知识图谱接进 Schema Linking（把注释兑现）

`KnowledgeGraph` 和 `related_tables()` 早就写好了，注释说"供 Schema Linking 增强"——但 `SchemaLinker`
**从没接过它**。本次补上，并给图谱补了「业务术语 → 物理表」的对应表关系（实验室→labs、设备→equipment…）：

- 实测收益：`各实验室的设备利用率` 这类问题，物理表名是英文（labs/equipment），纯字面匹配**一张表都抽不到**，
  现在靠图谱的「实验室→labs、设备→equipment」正确落表（`linker.last_reasons` 可解释，图编排的 trace 会显示
  "知识图谱贡献 N 张"）。
- 设计约束：只走一跳的「对应表」关系，不做多跳推理——多跳会把无关表拉进 prompt，反而稀释注意力。
- 单测：`tests/test_linker.py`（图谱命中/关闭时行为不变/概念关系不污染候选表）。

### 13.3 元数据接入：`MetadataProvider`（表结构的单一事实来源）

Demo 形态表结构手写在 `examples/grg_schema.py`；生产必须从元数据中心/数仓 API 自动获取。
`nl2sql/metadata.py` 提供三件事：

| 能力 | 说明 |
|---|---|
| `MetadataProvider.load()` | `Static`（代码内领域库，默认）/ `Api`（`GET {base}/tables`，Bearer 鉴权，进程内 TTL 缓存）两种实现，`METADATA__PROVIDER=api` 一键切换，业务代码零改动 |
| `fingerprint()` | 结构指纹：加表/删表/加列/改类型任一变化都会改变指纹，进程内检测到即告警 |
| `diff_tables()` | 把"变了什么"渲染成人话（新增/删除表、新增/删除列、类型变更） |

```bash
python examples/metadata_check.py --save    # 首次保存结构快照
python examples/metadata_check.py           # 有变更打印明细并退出码 1（CI 卡口）
```

配合上游的**库级只读 + 行级权限**（第 12 节）与这里的**schema 变更检测**，
"对接企业数据系统"的三件套就齐了：元数据自动获取、权限可执行、变更可发现。

---

## 14. 第二种执行引擎：一份 IR，编译到 SQL / ES DSL / PPL

对应 JD「自然语言转 **SQL/PPL/DSL**」。这里的关键设计不是"再写一套 prompt"，而是**中间表示（IR）**：

```
问题 ──(确定性规则填槽)──► QueryIR ──┬─► ES Query DSL ──► 真集群执行（阿里云 ES 9.3.2）
                                     └─► OpenSearch PPL ──► _plugins/_ppl 真执行（Aiven OpenSearch）
                                                              └ 普通 ES 上自动降级为"仅编译产物"
```

**为什么不让 LLM 直接写 ES JSON / PPL**：那等于每种语言各赌一次格式化（括号、保留字、字段名全靠模型），
且校验、权限、审计都要重写三遍。有了 IR，LLM/规则只负责**填槽**，各语言的语法正确性由**编译器**保证；
校验与行级权限作用在 IR 上——换执行引擎不换安全等级。

| 文件 | 职责 |
|---|---|
| `nl2sql/dsl.py` | `QueryIR` + 两个编译器：`to_es_dsl()` / `to_ppl()`，以及响应解析器 `parse_es_response()` |
| `nl2sql/es_backend.py` | REST 执行器：**只暴露 search/count/ping，没有任何写方法**，与只读承诺一致 |
| `examples/es_engine.py` | 问题→IR 的确定性规则 + `HybridRouter`（域路由 + 失败回落）+ 权限映射 |
| `examples/setup_es_demo.py` | 灌 1222 条设备日志，**按构造**生成（标准答案是生成时就写死的常量） |
| `examples/es_eval.py` | 9 条真机评估，`--min-accuracy` 可接 CI |

```bash
python examples/setup_es_demo.py      # 建索引 + 灌数据 + 打印标准答案
python examples/es_eval.py            # 真集群对拍
```

### 14.1 真机验证结果（阿里云 ES 9.3.2）

```
[PASS] E01 华东区最近7天的ERROR告警有多少条 -> 42
[PASS] E02 各区域最近7天的ERROR告警数量 -> {华东:42, 华南:30, 华北:25}
[PASS] E03 各区域最近30天的ERROR告警数量 -> {华东:60, 华南:42, 华北:35}
[PASS] E04 最近30天共有多少条ERROR告警 -> 137
[PASS] E05 包含「温度超限」的告警最近7天有多少条 -> 49
[PASS] E06 各实验室最近7天的ERROR告警数量 -> 3 行全中
[PASS] E07 告警最多的区域是哪个 -> 华东      [PASS] E08 告警最少 -> 华北
[PASS] E09 最近7天各级别有多少条日志 -> {ERROR:97, WARN:145, INFO:470}
通过 9/9 = 100.0%（单次 ~55ms）
```

标准答案不是"跑一遍记下来的数字"，而是**按构造推导**的：数据生成时就写死了各区域各级别的条数，
所以种子重灌、隔天重跑，答案依然精确成立（这也是为什么数据跨度刻意小于查询窗口——否则最老的文档会
滑出 `now-7d`，答案漂移，评估就不可重复了）。

### 14.2 三个真机踩坑（都写进了代码注释/测试，不是口口相传）

1. **公网入口是 `http` 不是 `https`**：写 `https://` 会 TLS 握手失败（`WRONG_VERSION_NUMBER` /
   `UNEXPECTED_EOF`），很容易被误判成"白名单没放行"。先试 http。
2. **别发 `compatible-with=N` 的 Accept 头**：本机实测 ES 9.3.2 对
   `application/vnd.elasticsearch+json; compatible-with=8`（**和 =9**）一律返回
   `400 media_type_header_exception`。客户端不该绑架集群版本——改用通用 `application/json`，7/8/9 通吃。
   （`tests/test_es_engine.py::test_headers_do_not_pin_es_major_version` 守住这条）
3. **公网访问白名单为空**：阿里云 ES 默认不放行任何 IP，TCP 9200 直接不通。

### 14.3 路由与降级：加第二种引擎不能让既有能力变脆弱

`HybridRouter` 只在问题属于**事件流水域**（告警/异常/日志/事件）时走 ES，其余全部走原 SQL 引擎；
ES 报错时**自动回落 SQL**并在 `reasons` 里标注。行级权限由 `scope_filters_from_policy()` 从同一个
`DataPolicy` 映射成 ES `terms` 过滤——`labs.region → region`、`business_lines.code → business_line`，
**换引擎不换安全等级**。

### 14.4 PPL 真执行：Aiven OpenSearch 接入（同一份 IR，第二个后端）

`ElasticsearchBackend.execute_ppl()` 走 OpenSearch 的 `_plugins/_ppl` 端点；每次 `ask()` 都会
双路执行——ES DSL 与 PPL 各跑一遍，`ppl.status` 标注 `executed` / `compiled-only`：
后端探测不到 PPL 端点（普通 ES，如阿里云）就自动降级，主结果不受影响。

方言差异消化在**编译器**里（这正是 IR 方案的价值）：PPL 的 `where` 不认 ES 的 date math（`now-7d`），
`to_ppl()` 把相对窗口换算成执行时刻的绝对时间（`_abs_since()`，可注入 now 做离线单测）。

```bash
# 连 Aiven OpenSearch 时跑双路对拍（ES 侧与 PPL 侧分别和标准答案比对）
ES__HOST=https://noahdemo-noahdemo.b.aivencloud.com:26380 python examples/es_eval.py
```

> 面试价值：**「PPL 你真跑过吗？」** —— 跑过。同一份 IR 在阿里云 ES 上编译成 ES DSL 真执行，
> 在 Aiven OpenSearch 上编译成 PPL 经 `_plugins/_ppl` 真执行，两条路径各自与标准答案对拍；
> PPL 不可用时自动降级为编译产物并如实标注。把"能不能跑"和"能不能编译"分开讲，
> 且两张成绩单都拿得出，比任何单引擎故事都硬。

---

## 15. 真实业务数据：让 Demo 变成产品原型

前 14 节的骨架是通用的，但问数系统的"灵魂"在**数据**。这一节灌入广电计量业务背景的数据，
覆盖财务经营、实验室资源、设备日志、检测报告四类，分别落到 PG 与 ES/OpenSearch。

| 数据类别 | 目标库 | 内容 | 支撑的问数场景 |
|---|---|---|---|
| 业务板块经营 | PostgreSQL `business_segment_revenue` | 10 个季度 × 7 板块的营收/同比/毛利率（年营收约 32 亿量级） | 经营分析、同比、毛利率对比 |
| 实验室资源 | PostgreSQL `labs`（扩列 + 扩点） | 20 个全国基地的成立年份/设备台数/在职人数（合计 4415 台 / 3315 人） | 实验室运营监控、资源统计 |
| 设备日志 | ES/OpenSearch `device_events` | 1222 条事件流水 | 告警计数、分组、TopN（PPL/DSL） |
| 检测报告文本 | ES/OpenSearch `inspection_reports` | 2000 份报告全文（20 类检测项目 × 15 类标准依据） | 全文检索、知识库 RAG 语料 |

业务口径按广电计量（002967）的真实盘面模拟：年营收约 32 亿、计量校准与可靠性与环境试验是两大主力
（合计约六成）、全国 20+ 基地、检测目录覆盖计量/可靠性/EMC/集成电路/软件测评/生命科学/EHS。

```bash
python examples/seed_business_data.py     # PG：板块经营 + 实验室资源（打印标准答案）
python examples/setup_es_demo.py          # ES：设备日志
python examples/seed_report_docs.py       # ES：检测报告全文
```

### 15.1 关键决策：**维度真实、度量按构造**（不用 faker 撒点）

常见做法是 `faker` + `random.randint` 一把梭，那对"看起来有数据"够用，但会**毁掉评估体系**——
标准答案每天变，execution accuracy 无从谈起，系统就从"可衡量的产品原型"退回"演示品"。

这里的做法：板块名/城市/实验室/检测项目用**真实业务词表**，营收/毛利率这些度量由**确定性公式**
生成，脚本**打印标准答案**供评估集直接写标准 SQL：

```
# 最近季度 2026-06-30：计量服务 25067.52 / 可靠性与环境试验 21420.0 / 集成电路 10755.9（同比 5.21%）
# 营收最高板块: 计量服务      毛利率最高: 软件测评 61.8%      2025 全年营收合计: 328224.0（约 32.8 亿）
# 实验室网络: 20 个基地，设备合计 4415 台，人员 3315 人；各区域设备: 华南1455/华东1200/华北800...
```

配套两个刻意设计：
- **各板块增速差异化**（集成电路/软件测评高增、计量/EHS 平稳）。若都用同一增速，
  "哪个板块增长最快"会并列无解——这类问句演示时必被问到。
- **数据时间跨度小于查询窗口**，隔天重跑评估答案不漂移（可重复性是评估集的生命线）。

零新增依赖：不引 faker（随机不可控）、不引 elasticsearch-py/opensearch-py（httpx 直连即可，
且客户端不该绑架集群版本——见 §14.2 踩坑 2）。

### 15.2 数据灌进来之后，评估集抓出了三个真问题

新增 8 条经营类用例后首测 6/8，每个失败都是真实缺陷，且**都是指标治理问题**：

| 现象 | 根因 | 治理动作 |
|---|---|---|
| 「所有实验室的设备总数」返回 5 行分组而非总数 | 语义层把"所有"当成分组触发词 | `GROUP_TRIGGERS` 移除"所有"（它是全称限定，语义是"合起来一共"） |
| 「各实验室设备台数」用 `COUNT(equipment.id)`（1~2 台） | 口径未登记：台账口径 vs 资源统计口径 | 立 `equipment_stock` = `SUM(labs.equipment_count)`，别名与"多少台设备"（COUNT 口径）**切开** |
| 「已开票的合同总金额」走错指标/掉进 RAG | "合同总金额"曾挂在收入指标上，与合同直加口径冲突 | 立 `contract_amount` = `SUM(contracts.amount)`，与"检测服务收入"（需关联已出具报告）显式区分 |

> 这三条正是 JD「协同优化**指标体系**」的活案例：**不是模型不够聪明，是指标没定义清楚**。
> 评估集负责暴露，语义层负责治理，前后分数可量化——之前记为"已知失败"的 E04 双口径冲突，
> 治理后已通过。

### 15.2.1 口径审计（自查发现：跑分自洽 ≠ 数字正确）

评估集能抓住"模型答错"，但**抓不住"标准答案本身就错"**——因为语义层提示词与标准 SQL
出自同一套口径，两边一起错，跑分反而永远绿色。这轮做了一次"把真库数字与业务直觉对照"的审计，
查出四类问题（都已修）：

| # | 问题 | 实证 | 修法 |
|---|---|---|---|
| 1 | **合同金额被重复累加**：合同 → 委托单 → 报告 是一对多，`SUM(c.amount)` 等于"金额 × 报告数" | 客户维度真实合计 250 万，标准答案写成 1,444 万（约 10 倍）；收入指标 205 万 → 490 万 | 收入口径改为"先按合同去重再求和"（`SELECT DISTINCT c.id, c.amount` 子查询），语义层 `definition`/`sql_hint`/示例 SQL、评估集 `t_revenue` 全部同步 |
| 2 | **标准答案与问题口径不一致**：C05 问"合同总金额"（未提开票），标准却过滤 `已开票` 且重复累加 | 系统按字面回答 157 万/93 万，反被判错 —— 它是当前**唯一失败的用例**，其实是**用例错了** | 标准 SQL 改为合同表直接汇总；`contract_amount` 口径补"禁止 JOIN 委托单/报告"警示 |
| 3 | **`on_time` 与它自己声明的口径不自洽**：指标定义写"按期 = 出具日 ≤ 承诺日"，但 53 行 `on_time=1` 按下标算是逾期 | 一旦 LLM 依定义用日期比较生成 SQL，准时率会与用 `on_time` 列算的**完全不同** | 种子数据改为按 `on_time` 反推 `promised_date`（按期=承诺晚 4 天，逾期=早 3 天），并把 `created_at` 提前到 95 天前；审计后不符行数 = 0 |
| 4 | **时间维度形同虚设**：报告全部落在 30 天内，"上个月 / 不限时间"结果完全相同 | 多轮演示「那最近 30 天呢」看不出变化 | 报告出具时间跨度拉到 10~50 天（近 30 天 40 条 vs 全部 77 条），同时保持"最近 7 天 = 0"这个边界用例 |

> 顺带修掉一个由此暴露的**权限绕过**：给示例 SQL 加子查询时漏了闭合括号 → sqlglot 解析失败 →
> 当时的列级权限检查"解析失败即跳过"→ 含敏感字段的 SQL 被回退路径直接执行。
> 已改：① 权限检查 **fail-closed**（配了敏感字段时解析失败即拒绝）；
> ② 模板回退同样先过 `validator`；③ 新增 `tests/test_assets.py` 对全部示例/指标 SQL
> 做"可解析 + 可校验 + 无敏感字段"的**资产体检**——这类错误不会再静默溜过单测。

### 15.3 面试话术

> "为了让 Demo 贴近真实业务，我按广电计量的业务口径设计了一套数据：PostgreSQL 里是分板块的
> 季度经营数据和实验室资源，ES/OpenSearch 里是设备日志和检测报告全文。但我没有用 faker 随机撒点——
> 维度真实、度量按构造，这样标准答案可推导，65 条评估集才能持续跑 execution accuracy。
> 数据灌进来之后，评估集又抓出三个指标口径问题，我在语义层逐个立指标、切别名解决掉了。"

**数据合规**：数据为按公开业务口径**模拟生成**（非真实经营数据）；生产环境应通过正式数据接口
或客户授权获取，并在落库前完成脱敏与分级。
