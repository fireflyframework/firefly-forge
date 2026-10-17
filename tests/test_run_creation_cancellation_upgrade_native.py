# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PREPARED ONLY: actual populated receipt-v1 schema27 to additive control schema28."""

import hashlib
import importlib.util
from pathlib import Path

from sqlalchemy import text
from test_campaign_delivery_plans import prepared
from test_run_creation_cancellation_native import cancellation
from test_run_creation_requests_native import registered
from test_run_creation_upgrade_native import snapshot

from firefly_forge.migrations import migrate
from firefly_forge.project_access import CURRENT_PRINCIPAL
from firefly_forge.run_creation_requests import RunCreationRequests


def previous_module(name, expected):
    source = Path(__file__).parent / "fixtures" / "run_creation" / (name + ".py")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == expected
    spec = importlib.util.spec_from_file_location("owned_" + name, source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_populated_actual_schema27_to28_keeps_v1_admission_and_execution_history_exact(
    store_factory, tmp_path, monkeypatch
):
    import firefly_forge.store as store_module

    old_migrations = previous_module(
        "schema27_migrations", "b89003ce030c6ff33083631cfbf03e63ab1970521f9e769ebcbed6beaa1edc64"
    )
    old_requests = previous_module(
        "schema27_requests", "8874480e877ad957f8612005bef3829ec793ef3563a5e79b47b475a2d950ced5"
    )
    assert old_migrations.SCHEMA_REVISION == 27
    monkeypatch.setattr(store_module, "migrate", old_migrations.migrate)
    store = await store_factory(tmp_path / "schema27" / "factory.db")
    async with store._unit() as session:
        assert await session.scalar(text("SELECT max(revision) FROM forge_schema_migrations")) == 27
        assert await session.scalar(text("SELECT to_regclass('forge_run_creation_reservations')")) is None
        assert await session.scalar(text("SELECT to_regclass('forge_run_creation_cancellations')")) is None
    c = await prepared(store, tmp_path)
    try:
        delivery = await c.plans.save(c.project["id"], c.campaign["id"], c.body)
        run_id = c.campaign["children"][0]["run_id"]
        await c.journal.ensure_plan(run_id)
        await c.journal.intent(run_id, "verify")
        async with registered(store, tmp_path) as (_, _, body):
            request_id = "e" * 32
            receipt, created = await old_requests.RunCreationRequests(store).admit(request_id, body)
            assert created
            before = await snapshot(store)
            assert all(before.values())
            async with store._unit() as session:
                retained = await session.scalar(
                    text("SELECT data::text FROM forge_run_creation_requests WHERE id=:id"), {"id": request_id}
                )
            monkeypatch.setattr(store_module, "migrate", migrate)
            async with store._unit() as session:
                await migrate(session, store.schema)
                await migrate(session, store.schema)
                assert list(
                    await session.scalars(text("SELECT revision FROM forge_schema_migrations ORDER BY revision"))
                ) == [27, 28]
                assert await session.scalar(text("SELECT count(*) FROM forge_run_creation_reservations")) == 0
                assert await session.scalar(text("SELECT count(*) FROM forge_run_creation_cancellations")) == 0
                assert (
                    await session.scalar(
                        text("SELECT data::text FROM forge_run_creation_requests WHERE id=:id"), {"id": request_id}
                    )
                    == retained
                )
            assert await snapshot(store) == before
            manager = RunCreationRequests(store)
            assert await manager.get(request_id) == receipt
            assert await manager.admit(request_id, body) == (receipt, False)
            decision = {"version": 1, "outcome": "admitted", "receipt": receipt}
            assert await manager.decision(request_id) == decision
            assert await manager.cancel(request_id, cancellation(body.model_dump(exclude_unset=True))) == (
                decision,
                False,
            )
            async with store._unit() as session:
                assert await session.scalar(text("SELECT count(*) FROM forge_run_creation_reservations")) == 0
                assert await session.scalar(text("SELECT count(*) FROM forge_run_creation_cancellations")) == 0
        assert await c.history.get(c.project["id"], c.campaign["id"], c.archive["request_id"]) == c.archive
        assert await c.plans.get(c.project["id"], c.campaign["id"], c.body.request_id) == delivery
        assert c.journal.verify_chain(await c.journal.entries(run_id))
    finally:
        CURRENT_PRINCIPAL.reset(c.token)
