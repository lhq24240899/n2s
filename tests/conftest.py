"""pytest 公共 fixtures：示例知识库 + 测试替身，无需任何外部依赖。"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest

from examples.schema import build_glossary, build_registry, build_store
from tests.doubles import MockDBRunner, MockLLM
from nl2sql.pipeline import Text2SQLPipeline


@pytest.fixture
def registry():
    return build_registry("postgres")


@pytest.fixture
def store():
    return build_store()


@pytest.fixture
def glossary():
    return build_glossary()


@pytest.fixture
def pipeline(registry, store, glossary):
    llm = MockLLM()
    db = MockDBRunner(registry)
    return Text2SQLPipeline(
        registry, store, llm, db, glossary, top_k=5, min_score=1.0, max_retry=1
    )
