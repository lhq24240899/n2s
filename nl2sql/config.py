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
