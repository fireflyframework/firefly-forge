# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real Journal recovery with transactional memory; physical SQL is a separate gate."""
# ruff: noqa: F811

import asyncio
import copy
from uuid import uuid4

import pytest
from test_checkpoint_mode_adoption_host import mode_case, request  # noqa: F401
from test_original_selection_proof_host import case as selection_proof_case
from test_sandbox_command_ordinals_host import command_case  # noqa: F401

from firefly_forge.checkpoint_mode_runtime import mode_adoption_scope, prepare_current_mode
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal, digest, valid_recovery_annotation
from firefly_forge.original_selection_proof import prepare_selection_readmission

REASON = "Worker stopped without a durable receipt; inspect the retained source and external target"


@pytest.fixture
async def recovery_case(mode_case, monkeypatch):
    c = mode_case

    async def pending():
        return [row for row in c.store.tasks.rows if row.status in {"running", "needs_reconciliation"}]

    monkeypatch.setattr(c.journal.tasks, "find_pending_recovery", pending, raising=False)
    assert await c.journal.recover() == 1
    c.old_recovery = copy.deepcopy((await c.journal.entries(c.run_id))[-1])
    assert c.old_recovery["payload"] == {"reason": REASON}
    assert c.old_recovery["idempotency_key"] == c.store.tasks.rows[0].data["idempotency_key"]
    candidate = await c.service.candidate(c.run_id)
    await c.service.request(c.run_id, uuid4().hex, request(candidate))
    prepared = await prepare_current_mode(c.journal, c.run_id)
    engine = object.__new__(Engine)
    engine.store, engine.journal, engine.assert_ownership = c.store, c.journal, c.store.assert_ownership
    with mode_adoption_scope(prepared):
        begun = await engine._begin_task(c.run_id, "implement", c.workspace, {})
        assert begun["disposition"] == "execute"
    c.store.run.status = "failed"
    c.store.run.data.update(status="failed", error="Controlled predispatch failure")
    c.claim = copy.deepcopy((await c.journal.entries(c.run_id))[-1])
    assert c.claim["kind"] == "checkpoint_mode_claim"
    return c


def state(c):
    return copy.deepcopy(
        {
            "run": c.store.run.data,
            "run_status": c.store.run.status,
            "tasks": [vars(row) for row in c.store.tasks.rows],
            "entries": [row.data for row in c.store.records.rows],
        }
    )


@pytest.mark.parametrize("initial", ["running", "needs_reconciliation"])
async def test_later_same_attempt_claim_gets_one_fresh_recovery_without_history_reset(recovery_case, initial):
    c = recovery_case
    task = c.store.tasks.rows[0]
    task.status = initial
    before = state(c)
    assert await c.journal.recover() == 1
    entries = await c.journal.entries(c.run_id)
    assert entries[:-1] == before["entries"]
    assert entries[-1]["sequence"] == c.claim["sequence"] + 1
    assert entries[-1]["kind"] == "recovery"
    assert entries[-1]["idempotency_key"] != task.data["idempotency_key"]
    assert len(entries[-1]["idempotency_key"]) == 64
    assert entries[-1]["payload"] == {
        "version": 1,
        "reason": REASON,
        "task_key": task.data["idempotency_key"],
        "generation": {k: c.claim[k] for k in ("kind", "idempotency_key", "digest")},
        "previous_recovery_digest": c.old_recovery["digest"],
        "observation": initial,
    }
    assert task.status == "needs_reconciliation"
    assert task.data == before["tasks"][0]["data"] and task.attempt == before["tasks"][0]["attempt"]
    assert c.store.run.data == before["run"] and c.store.run.status == "failed"
    assert Journal.verify_chain(entries)
    assert await c.journal.recover() == 0
    assert await c.journal.entries(c.run_id) == entries


async def test_two_recovery_observers_commit_one_annotation_for_the_same_claim(recovery_case):
    c = recovery_case
    before = await c.journal.entries(c.run_id)
    counts = await asyncio.gather(c.journal.recover(), c.journal.recover())
    assert sum(counts) == 1
    entries = await c.journal.entries(c.run_id)
    assert len(entries) == len(before) + 1 and entries[:-1] == before
    assert c.store.tasks.rows[0].status == "needs_reconciliation"


async def test_annotation_write_failure_rolls_back_task_and_journal(recovery_case, monkeypatch):
    c = recovery_case
    before = state(c)
    save = c.journal.records.save

    async def fail(row):
        if row.kind == "recovery" and row.idempotency_key != c.old_recovery["idempotency_key"]:
            raise RuntimeError("Controlled new annotation failure")
        return await save(row)

    monkeypatch.setattr(c.journal.records, "save", fail)
    with pytest.raises(RuntimeError, match="Controlled new annotation"):
        await c.journal.recover()
    assert state(c) == before


async def test_already_needs_annotation_repair_never_saves_the_existing_task(recovery_case, monkeypatch):
    c = recovery_case
    c.store.tasks.rows[0].status = "needs_reconciliation"
    before = state(c)

    async def refuse_task_change(row):
        raise AssertionError("An existing needs-reconciliation row must remain byte-exact")

    monkeypatch.setattr(c.journal.tasks, "save", refuse_task_change)
    assert await c.journal.recover() == 1
    after = state(c)
    assert after["tasks"] == before["tasks"] and after["run"] == before["run"]
    assert after["entries"][:-1] == before["entries"]


@pytest.mark.parametrize("status", ["running", "needs_reconciliation"])
async def test_invalid_occupied_generation_identity_never_changes_task_or_journal(recovery_case, status):
    c = recovery_case
    assert await c.journal.recover() == 1
    row = c.store.records.rows[-1]
    row.data["payload"]["version"] = True
    row.data["digest"] = digest({k: v for k, v in row.data.items() if k != "digest"})
    row.digest = row.data["digest"]
    c.store.tasks.rows[0].status = status
    before = state(c)
    assert Journal.verify_chain(await c.journal.entries(c.run_id))
    with pytest.raises(ValueError, match="Recovery generation"):
        await c.journal.recover()
    assert state(c) == before


async def test_later_native_intent_preserves_its_prior_attempt_claim_metadata(recovery_case):
    c = recovery_case
    assert await c.journal.recover() == 1
    old = copy.deepcopy(c.store.tasks.rows[0].data)
    await c.journal.reconcile(
        c.run_id,
        old["idempotency_key"],
        evidence={"inspection": "Controlled local effect had no dispatch"},
        outcome="retry",
    )
    intent = await c.journal.intent(c.run_id, "implement", 3, {"reason": "Controlled explicit reconciliation"})
    assert intent["disposition"] == "execute" and intent["idempotency_key"] != old["idempotency_key"]
    before = await c.journal.entries(c.run_id)
    assert await c.journal.recover() == 1
    task = c.store.tasks.rows[0]
    assert task.status == "needs_reconciliation" and task.attempt == 3
    assert task.data["checkpoint_mode_claim_digest"] == old["checkpoint_mode_claim_digest"]
    assert (await c.journal.entries(c.run_id))[:-1] == before


def versioned_selection_case():
    c = selection_proof_case()
    task, entries = c["task"], c["entries"]
    generation = entries[-1]
    task["status"] = "needs_reconciliation"
    key = digest(
        {
            "version": 1,
            "kind": "recovery",
            "run_id": c["run"]["id"],
            "task_id": "implement",
            "task_key": task["idempotency_key"],
            "generation_digest": generation["digest"],
        }
    )
    record = {
        "run_id": c["run"]["id"],
        "sequence": len(entries) + 1,
        "task_id": "implement",
        "idempotency_key": key,
        "kind": "recovery",
        "payload": {
            "version": 1,
            "reason": REASON,
            "task_key": task["idempotency_key"],
            "generation": {k: generation[k] for k in ("kind", "idempotency_key", "digest")},
            "previous_recovery_digest": None,
            "observation": "running",
        },
        "previous_hash": generation["digest"],
    }
    record["digest"] = digest(record)
    entries.append(record)
    return c


def test_original_selection_accepts_only_the_fresh_versioned_mode_claim_annotation():
    c = versioned_selection_case()
    proof = prepare_selection_readmission(**c)
    assert proof["startup_recovery_digest"] == c["entries"][-1]["digest"]


@pytest.mark.parametrize("change", ["claim", "task", "previous", "observation", "key", "bool_version", "extra"])
def test_versioned_recovery_cannot_substitute_another_generation_or_identity(change):
    c = versioned_selection_case()
    record = c["entries"][-1]
    if change == "claim":
        record["payload"]["generation"]["digest"] = "0" * 64
    elif change == "task":
        record["payload"]["task_key"] = "0" * 32
    elif change == "previous":
        record["payload"]["previous_recovery_digest"] = "0" * 64
    elif change == "observation":
        record["payload"]["observation"] = "done"
    elif change == "bool_version":
        record["payload"]["version"] = True
    elif change == "extra":
        record["payload"]["grant_retry"] = True
    else:
        record["idempotency_key"] = c["task"]["idempotency_key"]
    record["digest"] = digest({k: v for k, v in record.items() if k != "digest"})
    assert Journal.verify_chain(c["entries"])
    with pytest.raises(ValueError):
        prepare_selection_readmission(**c)


def test_legacy_recovery_cannot_cross_native_run_boundaries():
    c = versioned_selection_case()
    record = c["entries"][-1]
    record.update(run_id="9" * 32, idempotency_key=c["task"]["idempotency_key"], payload={"reason": REASON})
    record["digest"] = digest({k: v for k, v in record.items() if k != "digest"})
    assert Journal.verify_chain(c["entries"])
    assert not valid_recovery_annotation(record, c["entries"], "implement", c["task"]["idempotency_key"])


def test_boolean_claim_version_is_not_a_native_integer_generation():
    c = versioned_selection_case()
    claim, record = c["entries"][-2:]
    claim["payload"]["version"] = True
    claim["digest"] = digest({k: v for k, v in claim.items() if k != "digest"})
    c["task"]["checkpoint_mode_claim_digest"] = claim["digest"]
    record["previous_hash"] = claim["digest"]
    record["payload"]["generation"]["digest"] = claim["digest"]
    record["idempotency_key"] = digest(
        {
            "version": 1,
            "kind": "recovery",
            "run_id": c["run"]["id"],
            "task_id": "implement",
            "task_key": c["task"]["idempotency_key"],
            "generation_digest": claim["digest"],
        }
    )
    record["digest"] = digest({k: v for k, v in record.items() if k != "digest"})
    assert Journal.verify_chain(c["entries"])
    with pytest.raises(ValueError):
        prepare_selection_readmission(**c)


def test_recovery_repository_semantics_are_material_to_both_native_contracts(monkeypatch):
    from pathlib import Path

    from firefly_forge.agentic_checkpoint_invocation import runtime_contract
    from firefly_forge.source_budget_allowances import admission_contract_digest

    before_admission, before_runtime = admission_contract_digest(), runtime_contract()
    read = Path.read_bytes
    monkeypatch.setattr(
        Path, "read_bytes", lambda path: read(path) + b"\nchanged\n" if path.name == "persistence.py" else read(path)
    )
    assert admission_contract_digest() != before_admission
    assert runtime_contract() != before_runtime
