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


class DBSettings(BaseModel):
    dialect: str = "postgres"    # postgres / mysql / clickhouse（影响 sqlglot 解析与 EXPLAIN 语法）
    dsn: str = ""                # 必填；缺省时 build_db 直接抛错
    dry_run: bool = True         # 是否用 EXPLAIN / LIMIT 1 做执行预检
    timeout: float = 10.0


class RetrievalSettings(BaseModel):
    min_score: float = 1.0       # 低于该分的示例不进入候选，避免检索错误带偏 LLM
    top_k: int = 5


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
