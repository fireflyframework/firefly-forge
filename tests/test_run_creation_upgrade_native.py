# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared for the sole native operator: populated revision 26 to the current schema."""

import hashlib
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_campaign_delivery_plans import prepared
from test_run_creation_requests_native import REQUEST, registered

from firefly_forge.migrations import SCHEMA_REVISION, migrate
from firefly_forge.project_access import CURRENT_PRINCIPAL
from firefly_forge.run_creation_requests import RunCreationRequests

LEGACY_MIGRATOR_SHA256 = "138e56f45144ad1d25ab1eb5364dcd8bb19b6c9329ae5695d77bafa1f42d2326"
PRESERVED_TABLES = (
    "forge_runs",
    "forge_events",
    "forge_plans",
    "forge_journal",
    "forge_campaigns",
    "forge_campaign_events",
    "forge_campaign_report_archives",
    "forge_campaign_delivery_plan_revisions",
)


async def snapshot(store):
    async with store._unit() as session:
        return {
            table: list(await session.scalars(text(f"SELECT to_jsonb(row) FROM {table} AS row ORDER BY id")))
            for table in PRESERVED_TABLES
        }


async def test_populated_actual_schema26_upgrade_preserves_execution_and_receipts(store_factory, tmp_path, monkeypatch):
    import firefly_forge.store as store_module

    source = Path(__file__).parent / "fixtures" / "run_creation" / "schema26_migrations.py"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == LEGACY_MIGRATOR_SHA256
    spec = importlib.util.spec_from_file_location("owned_schema26_migrations", source)
    assert spec is not None and spec.loader is not None
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    assert legacy.SCHEMA_REVISION == 26 and SCHEMA_REVISION == 32
    monkeypatch.setattr(store_module, "migrate", legacy.migrate)
    store = await store_factory(tmp_path / "schema26" / "factory.db")
    async with store._unit() as session:
        assert await session.scalar(text("SELECT max(revision) FROM forge_schema_migrations")) == 26
        assert await session.scalar(text("SELECT to_regclass('forge_run_creation_requests')")) is None
    c = await prepared(store, tmp_path)
    try:
        delivery = await c.plans.save(c.project["id"], c.campaign["id"], c.body)
        run_id = c.campaign["children"][0]["run_id"]
        await c.journal.ensure_plan(run_id)
        await c.journal.intent(run_id, "verify")
        before = await snapshot(store)
        assert all(before[table] for table in PRESERVED_TABLES)
        monkeypatch.setattr(store_module, "migrate", migrate)
        async with store._unit() as session:
            await migrate(session, store.schema)
            await migrate(session, store.schema)
            revisions = list(
                await session.scalars(text("SELECT revision FROM forge_schema_migrations ORDER BY revision"))
            )
            assert revisions == [26, SCHEMA_REVISION]
            assert await session.scalar(text("SELECT to_regclass('forge_run_creation_requests')")) is not None
            assert await session.scalar(text("SELECT count(*) FROM forge_run_creation_requests")) == 0
        assert await snapshot(store) == before
        assert await c.history.get(c.project["id"], c.campaign["id"], c.archive["request_id"]) == c.archive
        assert await c.plans.get(c.project["id"], c.campaign["id"], c.body.request_id) == delivery
        assert c.journal.verify_chain(await c.journal.entries(run_id))
        async with registered(store, tmp_path) as (_, _, body):
            requests = RunCreationRequests(store)
            receipt, created = await requests.admit(REQUEST, body)
            assert created and (await requests.admit(REQUEST, body)) == (receipt, False)
            assert await requests.get(REQUEST) == receipt
            for statement in (
                "UPDATE forge_run_creation_requests SET data='{}'::jsonb WHERE id=:id",
                "DELETE FROM forge_run_creation_requests WHERE id=:id",
            ):
                with pytest.raises(DBAPIError):
                    async with store._unit() as session:
                        await session.execute(text(statement), {"id": REQUEST})
            assert await requests.get(REQUEST) == receipt
            async with store._unit() as session:
                assert await session.scalar(text("SELECT count(*) FROM forge_run_creation_requests")) == 1
    finally:
        CURRENT_PRINCIPAL.reset(c.token)
