# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native durable refresh schema and pagination, without a database."""


def test_records_have_durable_unique_identity_and_unbounded_cursor_inventory():
    from sqlalchemy.dialects import postgresql

    from firefly_forge.framework_knowledge_persistence import FrameworkKnowledgeRecord, FrameworkKnowledgeRecords

    table = FrameworkKnowledgeRecord.__table__
    assert table.name == "forge_framework_knowledge_records"
    assert any({c.name for c in item.columns} == {"kind", "key"} for item in table.constraints)
    sql = str(FrameworkKnowledgeRecords.page_query("source", "a" * 32, None, 200).compile(dialect=postgresql.dialect()))
    assert "parent" in sql and "ORDER BY" in sql and "LIMIT" in sql and "OFFSET" not in sql
    assert "framework" in table.columns and "revision" in table.columns
