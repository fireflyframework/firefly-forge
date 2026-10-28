# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real plan-selection history stays within one read-only PostgreSQL cut."""

import asyncio
import contextvars
import io
import json
import zipfile
from uuid import UUID, uuid4

from sqlalchemy import select, text
from test_journal import profiled_pause

from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserEntity
from firefly_forge.persistence import ForgePlanEntity, ForgeRunEntity, ForgeTaskEntity
from firefly_forge.project_access import CURRENT_PRINCIPAL
from firefly_forge.run_evidence import EvidenceQuery, RunEvidence


async def two_plans(store):
    journal, paused, _ = await profiled_pause(store)
    await journal.resume(paused["id"], "agentic", model_profile_id="next-builder")
    return journal, paused["id"]


async def plan_and_task_rows(store, run_id):
    async with store._unit() as session:
        plans = list(
            await session.scalars(
                select(ForgePlanEntity).where(ForgePlanEntity.run_id == UUID(run_id)).order_by(ForgePlanEntity.revision)
            )
        )
        tasks = list(
            await session.scalars(
                select(ForgeTaskEntity).where(ForgeTaskEntity.run_id == UUID(run_id)).order_by(ForgeTaskEntity.task_id)
            )
        )
        return (
            [(row.id.hex, row.revision, row.digest, row.data) for row in plans],
            [(row.id.hex, row.plan_id.hex, row.task_id, row.status, row.attempt, row.data) for row in tasks],
        )


async def test_native_two_plan_query_and_export_preserve_original_foreign_keys_without_writes(store):
    journal, run_id = await two_plans(store)
    actor = Principal(uuid4(), "local", "plan-history-reader", authentication_method="local_bootstrap")
    async with store._unit() as session:
        session.add(ForgeIdentityUserEntity(id=actor.id, issuer=actor.issuer, subject=actor.subject, enabled=True))
    context = CURRENT_PRINCIPAL.set(actor)
    try:
        before = await plan_and_task_rows(store, run_id)
        before_run = await store.get_run(run_id)
        before_wal = await journal.entries(run_id)
        assert len(before[0]) == 2
        expected_task_ids = sorted(task["id"] for task in before[0][0][3]["tasks"])
        assert [row[2] for row in before[1]] == expected_task_ids
        assert {row[1] for row in before[1]} == {before[0][0][0]}
        reader = RunEvidence(store)
        page = await reader.query(run_id, EvidenceQuery(limit=1))
        assert page["manifest"]["plan_digest"] == before[0][0][2]
        assert page["manifest"]["plan_history"]["latest_observed"]["digest"] == before[0][1][2]
        exported = await reader.export(run_id, page["manifest"]["digest"])
        assert exported == await reader.export(run_id, page["manifest"]["digest"])
        with zipfile.ZipFile(io.BytesIO(exported)) as archive:
            history = json.loads(archive.read("plans.json"))
            assert [row["revision"] for row in history["plans"]] == [1, 2]
            assert history["plans"][0]["full_plan_digest_in_wal"] is True
            assert history["plans"][1]["full_plan_digest_in_wal"] is False
            assert sorted(row["task_id"] for row in history["task_plan_bindings"]) == expected_task_ids
            assert {row["plan_id"] for row in history["task_plan_bindings"]} == {before[0][0][0]}
            assert json.loads(archive.read("journal.json")) == before_wal
            assert "private-next-key" not in b"".join(archive.read(name) for name in archive.namelist()).decode()
        assert await plan_and_task_rows(store, run_id) == before
        assert await store.get_run(run_id) == before_run
        assert await journal.entries(run_id) == before_wal
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_native_plan_history_repeatable_read_excludes_a_real_concurrent_selection_commit(store, monkeypatch):
    journal, run_id = await two_plans(store)
    before = await plan_and_task_rows(store, run_id)
    before_wal = await journal.entries(run_id)
    reader = RunEvidence(store)
    original = reader._rows
    advanced = False

    async def concurrent(session, statement, limit, remaining):
        nonlocal advanced
        rows = await original(session, statement, limit, remaining)
        if statement.column_descriptions[0]["entity"] is ForgeRunEntity and not advanced:
            advanced = True
            assert await session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
            assert await session.scalar(text("SHOW transaction_read_only")) == "on"

            async def advance():
                effect = await journal.intent(run_id, "implement", attempt=2)
                await journal.fail(run_id, effect["idempotency_key"], "quota", "Fictional selection pause")
                await journal.resume(run_id, "agentic", model_profile_id="builder")

            await asyncio.wait_for(asyncio.create_task(advance(), context=contextvars.Context()), 5)
        return rows

    monkeypatch.setattr(reader, "_rows", concurrent)
    frozen = await reader._capture(run_id)
    assert advanced
    assert frozen.manifest["plan_history"]["count"] == 2
    assert frozen.manifest["plan_history"]["latest_observed"]["digest"] == before[0][1][2]
    assert frozen.members["journal.json"] == before_wal
    assert {row["plan_id"] for row in frozen.members["plans.json"]["task_plan_bindings"]} == {before[0][0][0]}
    later = await reader._capture(run_id)
    assert later.manifest["plan_history"]["count"] == 3
    assert len(later.members["journal.json"]) == len(before_wal) + 3
    assert frozen.manifest["digest"] != later.manifest["digest"]
    assert [row[0:3] for row in (await plan_and_task_rows(store, run_id))[0]][:2] == [row[0:3] for row in before[0]]
