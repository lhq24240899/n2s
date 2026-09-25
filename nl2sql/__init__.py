"""__init__：包入口与版本。"""
from __future__ import annotations

__version__ = "0.1.0"

from .models import (
    GenerationResult,
    PipelineTrace,
    ResultSource,
    RetrievalHit,
    SQLExample,
    TableSchema,
)
from .pipeline import Text2SQLPipeline

__all__ = [
    "Text2SQLPipeline",
    "GenerationResult",
    "PipelineTrace",
    "ResultSource",
    "RetrievalHit",
    "SQLExample",
    "TableSchema",
    "__version__",
]
