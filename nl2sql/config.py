"""全局配置：pydantic-settings 加载 .env，结构化日志初始化。

生产环境用环境变量 / secrets manager 注入，不建议把 .env 提交到仓库。
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseModel):
    provider: str = "openai"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""            # 必填；缺省时 build_llm 直接抛错
    model: str = "gpt-4o-mini"
    temperature: float = 0.0     # 生成 SQL 必须确定性，温度恒为 0
    max_tokens: int = 1024
    timeout: float = 30.0
    # 忽略 HTTP_PROXY/HTTPS_PROXY 等环境代理（httpx trust_env=False）。
    # 本机代理软件未开、却残留代理环境变量时会导致 Connection error，置 true 可直连。
    disable_proxy: bool = False


class DBSettings(BaseModel):
    dialect: str = "postgres"    # postgres / mysql / clickhouse（影响 sqlglot 解析与 EXPLAIN 语法）
    dsn: str = ""                # 必填；缺省时 build_db 直接抛错
    dry_run: bool = True         # 是否用 EXPLAIN / LIMIT 1 做执行预检
    timeout: float = 10.0
    # 库级只读：连接后 SET default_transaction_read_only = on。
    # 与 SQLValidator 的 AST 白名单形成双保险——即使有 SQL 绕过静态校验，
    # 数据库也会直接拒绝写操作。生产务必保持 true。
    readonly: bool = True
    # 语句超时：防止一条慢查询占满连接（资源/算力保护）。
    statement_timeout_ms: int = 5000


class AuthSettings(BaseModel):
    """鉴权设置（FastAPI / MCP server 共用）。"""

    enabled: bool = True
    secret: str = ""             # HS256 密钥；生产必须注入固定随机值
    issuer: str = "nl2sql"
    audience: str = "nl2sql-agent"
    ttl_seconds: int = 3600
    # 仅本地演示：允许 /v1/auth/token 自助签发令牌（生产必须 false）
    dev_token_endpoint: bool = False


class ApiSettings(BaseModel):
    """服务层设置（限流 / 并发 / 会话 / 审计）。"""

    title: str = "计量检测智能问数 API"
    version: str = "1.0.0"
    # 每主体每分钟最大请求数（令牌桶，粗略但够用的算力保护）
    rate_limit_per_min: int = 30
    # 同时打到 LLM/DB 的最大请求数（超出排队，排队超时返回 429）
    max_concurrency: int = 4
    queue_timeout: float = 20.0
    # 会话：每会话独立多轮上下文；超时/超量自动清理
    session_ttl_seconds: int = 1800
    max_sessions: int = 200
    # 审计日志内存保留条数（同时按结构化日志输出，便于接 Loki/ES）
    audit_buffer: int = 500


class RetrievalSettings(BaseModel):
    min_score: float = 1.0       # 低于该分的示例不进入候选，避免检索错误带偏 LLM
    top_k: int = 5


class EmbeddingSettings(BaseModel):
    """向量化设置（用于企业知识库的混合检索）。

    base_url / api_key 留空则复用 LLM 的配置（同一个 OpenAI 兼容网关）。
    """

    model: str = "text-embedding-3-small"
    dim: int = 1536              # 必须与所选模型一致，换模型要同步改
    base_url: str = ""
    api_key: str = ""
    timeout: float = 30.0
    disable_proxy: bool = False  # 同 LLM：本机代理异常时置 true 直连


class KBSettings(BaseModel):
    """企业知识库（pgvector）设置。"""

    enabled: bool = True
    table: str = "kb_docs"
    top_k: int = 4               # 混合召回后注入 prompt 的文档条数
    rrf_k: int = 60              # RRF 融合常数（论文默认值）
    doc_max_chars: int = 1200    # 单条文档注入 prompt 的截断长度

    # SQL 示例库的向量索引（让"长尾问句"也能召回相近范例）
    example_table: str = "sql_example_vec"
    example_top_k: int = 4
    example_min_sim: float = 0.30  # 向量召回的相似度下限，避免引入无关范例

    # rerank 精排（RRF 融合之后再做一次相关性精排）
    rerank: bool = True
    rerank_candidates: int = 8   # 进入精排的候选条数
    rerank_min_score: float = 3.0  # 精排分数低于此值的候选被丢弃（避免无关资料污染 prompt）


class PipelineSettings(BaseModel):
    max_retry: int = 1           # 生成失败后的重试次数（每次携带错误反馈）
    critique_llm: bool = False   # 图编排下是否开启 LLM 结果复核（默认关，省成本）


class MetadataSettings(BaseModel):
    """元数据接入：表结构的来源（对应「对接企业数据系统」）。

    static = 代码内领域知识库（Demo 形态）；api = 从元数据中心/数仓 API 自动同步（生产形态）。
    """

    provider: str = "static"       # static | api
    base_url: str = ""             # api 模式：GET {base_url}/tables
    token: str = ""                # api 模式：Bearer token
    timeout: float = 10.0
    cache_ttl: float = 300.0       # api 模式的进程内缓存秒数


class LogSettings(BaseModel):
    level: str = "INFO"
    fmt: str = "text"            # text | json


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )
    llm: LLMSettings = LLMSettings()
    db: DBSettings = DBSettings()
    auth: AuthSettings = AuthSettings()
    api: ApiSettings = ApiSettings()
    metadata: MetadataSettings = MetadataSettings()
    retrieval: RetrievalSettings = RetrievalSettings()
    embedding: EmbeddingSettings = EmbeddingSettings()
    kb: KBSettings = KBSettings()
    pipeline: PipelineSettings = PipelineSettings()
    log: LogSettings = LogSettings()


def get_settings() -> Settings:
    return Settings()


class JsonFormatter(logging.Formatter):
    """极简 JSON 行日志，便于接入 Loki / ES 等采集系统。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "[%(asctime)s] %(levelname)-7s %(name)s | %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
