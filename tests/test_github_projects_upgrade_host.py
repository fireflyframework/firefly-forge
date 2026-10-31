# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Projects follows activity33 and preserves previously encrypted grant metadata."""

from types import SimpleNamespace

from firefly_forge import migrations
from firefly_forge.connection_oauth_persistence import OAuthAttemptEntity, OAuthEffectEntity, OAuthGrantEntity


async def test_activity33_upgrade_admits_projects_without_rewriting_history(monkeypatch):
    statements = []
    captured = []

    async def execute(statement, parameters=None):
        statements.append((str(statement), parameters))

    async def scalar(statement):
        return 33

    async def run_sync(action):
        action(object())

    async def connection():
        return SimpleNamespace(run_sync=run_sync)

    def create_all(connection, *, tables):
        captured.extend(table.name for table in tables)

    monkeypatch.setattr(migrations.ForgeRunEntity.metadata, "create_all", create_all)
    await migrations.migrate(
        SimpleNamespace(execute=execute, scalar=scalar, connection=connection), "forge_test_projects"
    )
    assert migrations.SCHEMA_REVISION == 34
    assert {
        "forge_engine_activity",
        "forge_github_projects_bindings",
        "forge_github_projects_configuration_receipts",
    } <= set(captured)
    assert any("forge_github_projects_configuration_receipts_immutable" in sql for sql, _ in statements)
    assert any(sql == "ALTER TABLE forge_oauth_grants ALTER COLUMN expires_at DROP NOT NULL" for sql, _ in statements)
    assert not any(
        "UPDATE forge_oauth_grants" in sql or "DROP TABLE" in sql or "TRUNCATE" in sql for sql, _ in statements
    )
    assert OAuthGrantEntity.__table__.c.expires_at.nullable
    assert not OAuthAttemptEntity.__table__.c.expires_at.nullable
    assert not OAuthEffectEntity.__table__.c.lease_expires_at.nullable
