# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Physical PostgreSQL recovery generations preserve task rows and durable history."""

import asyncio
import copy
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import inspect
from test_original_selection_readmission_sql import (
    native_selection_case,
)
from test_original_selection_readmission_sql import (
    test_native_original_selection_preserves_history_and_current_authority as verify_selection_delivery,
)

from firefly_forge.checkpoint_mode_adoptions import CheckpointModeAdoptions
from firefly_forge.journal import Journal
from firefly_forge.original_selection_readmissions import OriginalSelectionReadmissions

REASON = "Worker stopped without a durable receipt; inspect the retained source and external target"


@asynccontextmanager
async def earlier_recovery(monkeypatch):
    """Exercise the first native restart before the historical stronger-mode claim."""
    candidate = CheckpointModeAdoptions.candidate
    prior = {}

    async def after_restart(service, run_id):
        if run_id not in prior:
            assert await service.journal.recover() >= 1
            record = (await service.journal.entries(run_id))[-1]
            assert record["kind"] == "recovery" and record["payload"] == {"reason": REASON}
            prior[run_id] = copy.deepcopy(record)
        return await candidate(service, run_id)

    with monkeypatch.context() as patch:
        patch.setattr(CheckpointModeAdoptions, "candidate", after_restart)
        yield prior


async def task_state(c):
    async with c.store._unit():
        row = await c.journal._task(c.run_id, "implement")
        return {column.key: copy.deepcopy(getattr(row, column.key)) for column in inspect(type(row)).columns}


async def seed_missed_annotation(c):
    """Represent only the earlier startup bug inside this test's disposable schema."""
    async with c.store._unit():
        row = await c.journal._task(c.run_id, "implement")
        assert row.status == "running"
        row.status = "needs_reconciliation"
        await c.journal.tasks.save(row)


def assert_fresh_record(before, after, old, initial):
    claim, record = before[-1], after[-1]
    assert claim["kind"] == "checkpoint_mode_claim"
    assert after[:-1] == before and record["sequence"] == claim["sequence"] + 1
    assert record["kind"] == "recovery" and record["idempotency_key"] != old["idempotency_key"]
    assert len(record["idempotency_key"]) == 64
    assert record["payload"] == {
        "version": 1,
        "reason": REASON,
        "task_key": old["idempotency_key"],
        "generation": {k: claim[k] for k in ("kind", "idempotency_key", "digest")},
        "previous_recovery_digest": old["digest"],
        "observation": initial,
    }
    assert Journal.verify_chain(after)


@pytest.mark.parametrize("initial", ["running", "needs_reconciliation"])
async def test_real_sql_same_attempt_restart_appends_a_fresh_claim_witness(store, tmp_path, monkeypatch, initial):
    async with earlier_recovery(monkeypatch) as prior, native_selection_case(store, tmp_path, monkeypatch) as c:
        if initial == "needs_reconciliation":
            await seed_missed_annotation(c)
        prefix, before, run = await c.journal.entries(c.run_id), await task_state(c), await store.get_run(c.run_id)
        assert await c.journal.recover() == 1
        after = await task_state(c)
        assert_fresh_record(prefix, await c.journal.entries(c.run_id), prior[c.run_id], initial)
        assert after["status"] == "needs_reconciliation"
        if initial == "needs_reconciliation":
            assert after == before
        else:
            assert {k: v for k, v in after.items() if k not in {"status", "updated_at"}} == {
                k: v for k, v in before.items() if k not in {"status", "updated_at"}
            }
        assert await store.get_run(c.run_id) == run
        assert await c.journal.recover() == 0
        assert await task_state(c) == after


async def test_real_sql_two_connections_commit_one_recovery_generation(store, store_factory, tmp_path, monkeypatch):
    async with earlier_recovery(monkeypatch) as prior, native_selection_case(store, tmp_path, monkeypatch) as c:
        prefix = await c.journal.entries(c.run_id)
        peer = Journal(await store_factory(store.path))
        counts = await asyncio.gather(c.journal.recover(), peer.recover())
        assert sum(counts) == 1
        assert_fresh_record(prefix, await c.journal.entries(c.run_id), prior[c.run_id], "running")
        assert (await task_state(c))["status"] == "needs_reconciliation"


@pytest.mark.parametrize("initial", ["running", "needs_reconciliation"])
async def test_real_sql_append_failure_rolls_back_without_rewriting_old_history(store, tmp_path, monkeypatch, initial):
    async with earlier_recovery(monkeypatch) as prior, native_selection_case(store, tmp_path, monkeypatch) as c:
        if initial == "needs_reconciliation":
            await seed_missed_annotation(c)
        task, prefix, run = await task_state(c), await c.journal.entries(c.run_id), await store.get_run(c.run_id)
        save = c.journal.records.save

        async def fail(record):
            if record.kind == "recovery" and record.idempotency_key != prior[c.run_id]["idempotency_key"]:
                raise RuntimeError("Controlled physical annotation failure")
            return await save(record)

        with monkeypatch.context() as patch:
            patch.setattr(c.journal.records, "save", fail)
            with pytest.raises(RuntimeError, match="physical annotation"):
                await c.journal.recover()
        assert await task_state(c) == task
        assert await c.journal.entries(c.run_id) == prefix
        assert await store.get_run(c.run_id) == run


async def test_real_sql_missing_annotation_repairs_without_saving_the_existing_task(store, tmp_path, monkeypatch):
    async with earlier_recovery(monkeypatch), native_selection_case(store, tmp_path, monkeypatch) as c:
        await seed_missed_annotation(c)
        task = await task_state(c)

        async def refuse(record):
            raise AssertionError("Already-needs native task must remain byte-exact")

        with monkeypatch.context() as patch:
            patch.setattr(c.journal.tasks, "save", refuse)
            assert await c.journal.recover() == 1
        assert await task_state(c) == task


@pytest.mark.parametrize("boundary", ["complete", "restore"])
async def test_real_sql_versioned_recovery_continues_through_sdk_and_final_qa(store, tmp_path, monkeypatch, boundary):
    candidate = OriginalSelectionReadmissions.candidate
    observed = set()
    async with earlier_recovery(monkeypatch) as prior:

        async def after_restart(service, run_id):
            if run_id not in observed:
                before = await service.journal.entries(run_id)
                assert await service.journal.recover() == 1
                assert_fresh_record(before, await service.journal.entries(run_id), prior[run_id], "running")
                observed.add(run_id)
            return await candidate(service, run_id)

        with monkeypatch.context() as patch:
            patch.setattr(OriginalSelectionReadmissions, "candidate", after_restart)
            await verify_selection_delivery(store, tmp_path, monkeypatch, boundary)
        assert len(observed) == 1
