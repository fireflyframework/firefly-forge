# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual Journal transition; in-memory repository fixtures do not exercise SQL authorization."""

import copy
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_bridge_outcomes import MemoryStore, Rows

from firefly_forge.identity import Principal
from firefly_forge.journal import Journal, digest
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess

RUN, REQUEST = UUID(int=1).hex, UUID(int=2).hex


@pytest.fixture
def environment_journal(monkeypatch):
    store = MemoryStore()
    store.run.status = "failed"
    store.run.data = {
        "id": RUN,
        "status": "failed",
        "error": "Retained failure",
        "attempt": 1,
        "implementation_rounds": 1,
        "repair_budget": 2,
        "git_metadata_digest": "a" * 64,
        "execution_actor": {"user_id": UUID(int=3).hex},
    }
    task = SimpleNamespace(task_id="implement", status="queued", attempt=2, data={"idempotency_key": "prior"})
    journal = Journal(store, tasks=Rows([task]), records=Rows([]))
    actor = Principal(UUID(int=4), "fixture-issuer", "fixture-subject", api_roles=frozenset({"forge-admin"}))
    counts = {"ownership": 0, "execution": 0}

    async def owned():
        counts["ownership"] += 1

    async def authorized(*args):
        counts["execution"] += 1

    async def administrator(*args):
        return True

    store.assert_ownership = owned
    monkeypatch.setattr(ProjectAccess, "authorize_execution", authorized)
    monkeypatch.setattr(ProjectAccess, "current_administrator", administrator)
    snapshot = {
        **ProjectAccess.actor_snapshot(actor),
        "identity_digest": digest({"issuer": actor.issuer, "subject": actor.subject}),
    }
    candidate = {"previous_run_digest": digest(store.run.data)}
    candidate["digest"] = digest(candidate)
    receipt = {
        "version": 1,
        "request": {
            "request_id": REQUEST,
            "candidate_digest": candidate["digest"],
            "acknowledge_environment_only": True,
            "acknowledge_resume_existing_task": True,
        },
        "actor": snapshot,
        "candidate": candidate,
        "previous_task": {"id": "implement", "status": "queued", "attempt": 2, **task.data},
        "effect": {"metadata_before": "a" * 64, "metadata_after": "b" * 64},
    }
    return journal, store, task, actor, receipt, counts


async def record_receipt(journal, receipt):
    intent = await journal._append(
        RUN,
        "implement",
        REQUEST,
        "workspace_reconciliation_intent",
        {k: v for k, v in receipt.items() if k != "effect"},
    )
    receipt = {**receipt, "intent_digest": intent["digest"]}
    return await journal._append(RUN, "implement", REQUEST, "workspace_reconciliation_receipt", receipt)


async def test_queue_requires_native_receipt_and_preserves_existing_attempt_and_all_counters(environment_journal):
    journal, store, task, actor, receipt, counts = environment_journal
    old = copy.deepcopy(store.run.data)
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        event = await record_receipt(journal, receipt)
        result = await journal.queue_environment_reconciled(RUN, REQUEST)
        assert result["status"] == "queued" and store.run.status == "queued"
        assert result["git_metadata_digest"] == "b" * 64
        assert result["workspace_reconciliation"]["receipt_digest"] == event["digest"]
        assert task.status == "queued" and task.attempt == 2 and task.data == {"idempotency_key": "prior"}
        for field in ("attempt", "implementation_rounds", "repair_budget", "execution_actor"):
            assert result[field] == old[field]
        assert "retry_count" not in result
        entries = [row.data for row in journal.records.rows]
        assert Journal.verify_chain(entries)
        assert entries[-1]["kind"] == "workspace_reconciliation_queued"
        assert await journal.queue_environment_reconciled(RUN, REQUEST) == result
        assert len(journal.records.rows) == 3
        assert counts["ownership"] >= 2 and counts["execution"] >= 2
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.parametrize("change", ["no_receipt", "task", "run", "actor", "no_actor"])
async def test_queue_refuses_missing_receipt_drift_or_unrelated_authority(environment_journal, change):
    journal, store, task, actor, receipt, _ = environment_journal
    if change != "no_receipt":
        await record_receipt(journal, receipt)
    if change == "task":
        task.attempt = 3
    elif change == "run":
        store.run.data["repair_budget"] = 3
    elif change == "actor":
        actor = Principal(UUID(int=5), "fixture-issuer", "other-subject", api_roles=frozenset({"forge-admin"}))
    token = CURRENT_PRINCIPAL.set(None if change == "no_actor" else actor)
    old = copy.deepcopy(store.run.data)
    try:
        with pytest.raises((ValueError, AccessDenied)):
            await journal.queue_environment_reconciled(RUN, REQUEST)
        assert store.run.status == "failed" and store.run.data == old
        assert not any(row.kind == "workspace_reconciliation_queued" for row in journal.records.rows)
    finally:
        CURRENT_PRINCIPAL.reset(token)
