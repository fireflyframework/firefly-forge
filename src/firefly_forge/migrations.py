# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Versioned, additive startup migration for Forge's own PostgreSQL tables."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from firefly_forge.persistence import (
    ForgeDeploymentEntity,
    ForgeEventEntity,
    ForgeJournalEntity,
    ForgePlanEntity,
    ForgeRepositoryEntity,
    ForgeRunEntity,
    ForgeTaskEntity,
)

SCHEMA_REVISION = 3


async def migrate(session: AsyncSession, schema: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "forge:migration:" + schema}
    )
    await session.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    await session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    await session.execute(
        text(
            "CREATE TABLE IF NOT EXISTS forge_schema_migrations "
            "(revision INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
    )
    version = await session.scalar(text("SELECT max(revision) FROM forge_schema_migrations"))
    if version is not None and version > SCHEMA_REVISION:
        raise RuntimeError("The PostgreSQL schema requires a newer Forge release")
    if version is None or version < SCHEMA_REVISION:
        connection = await session.connection()
        tables = [
            ForgeRunEntity.metadata.tables[entity.__tablename__]
            for entity in (
                ForgeRepositoryEntity,
                ForgeRunEntity,
                ForgeEventEntity,
                ForgeDeploymentEntity,
                ForgePlanEntity,
                ForgeTaskEntity,
                ForgeJournalEntity,
            )
        ]
        await connection.run_sync(lambda sync: ForgeRunEntity.metadata.create_all(sync, tables=tables))
        await session.execute(
            text(
                "CREATE OR REPLACE FUNCTION forge_refuse_immutable_change() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                "RAISE EXCEPTION 'Forge execution history is immutable'; END; $$"
            )
        )
        for table in ("forge_plans", "forge_journal"):
            await session.execute(
                text(
                    f"CREATE OR REPLACE TRIGGER {table}_immutable "
                    f"BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW "
                    "EXECUTE FUNCTION forge_refuse_immutable_change()"
                )
            )
        await session.execute(
            text("INSERT INTO forge_schema_migrations(revision) VALUES (:revision)"), {"revision": SCHEMA_REVISION}
        )
