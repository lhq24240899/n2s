"""演示用知识库：两张表 + 两条带标签示例 + 一份口径。可替换为你真实的 schema。"""
from __future__ import annotations

from nl2sql.glossary import Glossary, Metric
from nl2sql.knowledge import SchemaRegistry, SQLExampleStore
from nl2sql.models import SQLExample, TableSchema


def build_tables() -> list[TableSchema]:
    return [
        TableSchema(
            name="orders",
            columns={
                "id": "int",
                "user_id": "int",
                "total_amount": "decimal",
                "status": "varchar",
                "created_at": "timestamp",
            },
            description="订单表",
            foreign_keys=[("user_id", "users", "id")],
            sample_values={"status": ["paid", "refunded", "pending"]},
        ),
        TableSchema(
            name="users",
            columns={"id": "int", "name": "varchar", "region": "varchar"},
            description="用户表",
        ),
    ]


def build_examples() -> list[SQLExample]:
    # 注意：示例 SQL 使用真实列名 total_amount，作为「可信模板」
    return [
        SQLExample(
            id="ex_gmv",
            question="昨天GMV是多少",
            sql=(
                "SELECT SUM(total_amount) AS gmv FROM orders "
                "WHERE status='paid' AND created_at >= CURRENT_DATE - 1"
            ),
            domain=["订单"],
            intent=["聚合"],
            tables=["orders"],
            metrics=["GMV"],
            dimensions=["日期"],
            keywords=["昨天"],
        ),
        SQLExample(
            id="ex_topn",
            question="消费前10的用户",
            sql=(
                "SELECT user_id, SUM(total_amount) AS total FROM orders "
                "WHERE status='paid' GROUP BY user_id ORDER BY total DESC LIMIT 10"
            ),
            domain=["订单"],
            intent=["TopN"],
            tables=["orders"],
            metrics=["消费额"],
            dimensions=["用户"],
            keywords=["前", "top"],
        ),
    ]


def build_glossary() -> Glossary:
    return Glossary(metrics=[Metric("GMV", "SUM(orders.total_amount) WHERE orders.status='paid'")])


def build_registry(dialect: str = "postgres") -> SchemaRegistry:
    return SchemaRegistry(build_tables(), dialect=dialect)


def build_store() -> SQLExampleStore:
    return SQLExampleStore(build_examples())
