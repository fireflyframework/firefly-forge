# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual Journal/RunService hold guards using in-memory repositories only."""

import copy

import pytest
from test_bridge_inflight_cancel import RUN, MemoryStore, Rows

from firefly_forge.controllers import RunService
from firefly_forge.journal import Journal


@pytest.mark.parametrize("status", ["needs_reconciliation", "interrupted", "cancelled", "failed"])
@pytest.mark.parametrize("entry", ["resume", "handoff", "auto", "retry"])
async def test_durable_completion_hold_never_allows_graph_reentry(tmp_path, status, entry):
    store = MemoryStore(tmp_path)
    store.run.data.update(
        status=status,
        bridge_completion_hold={
            "version": 1,
            "reason": "graph_checkpoint_unavailable",
            "request_id": "fbr1_" + "a" * 32,
            "intent_digest": "b" * 64,
        },
    )
    store.run.status = status
    before = copy.deepcopy(store.run.data)
    journal = Journal(store, plans=Rows(), tasks=Rows(), records=Rows())
    with pytest.raises(ValueError, match="graph checkpoint"):
        if entry == "retry":
            await RunService(store, None).retry(RUN)
        else:
            await journal.resume(RUN, "claude" if entry == "handoff" else None, auto=entry == "auto")
    assert store.run.data == before and store.created == [] and journal.records.rows == []


@pytest.mark.parametrize("task_id", ["implement", "review"])
async def test_known_refusal_retains_original_intent_without_quota_or_reset(tmp_path, task_id):
    store = MemoryStore(tmp_path)
    if task_id == "review":
        store.run.data["assurance"] = {"review_agent": "bridge"}
    journal = Journal(store, plans=Rows(), tasks=Rows(), records=Rows())
    await journal.ensure_plan(RUN)
    effect = await journal.intent(RUN, task_id)
    observation = {
        "completion_request_id": "fbr1_" + "a" * 32,
        "intent_digest": "b" * 64,
        "principal_id": "fixture",
        "admission": "admission_refused",
        "code": "completion_recovery_full",
    }
    error = await journal.fail(
        RUN, effect["idempotency_key"], "bridge_admission_refused", "Known no-admission", observation=observation
    )
    assert error["idempotency_key"] == effect["idempotency_key"]
    assert error["payload"]["kind"] == "bridge_admission_refused"
    assert error["payload"]["provider_observation"] == observation
    assert error["payload"]["automatic"] is False and error["payload"]["retry_at"] is None
    assert "retry_after" not in error["payload"] and "job_id" not in observation
    assert store.run.status == "needs_reconciliation"
    assert Journal.verify_chain([row.data for row in journal.records.rows])
    assert await journal.fail(RUN, effect["idempotency_key"], "bridge_admission_refused", "Duplicate") == error
    for agent in (None, "claude"):
        with pytest.raises(ValueError, match="reconcil"):
            await journal.resume(RUN, agent)
    store.run.data["status"] = store.run.status = "cancelled"
    with pytest.raises(ValueError, match="reconcil"):
        await RunService(store, None).retry(RUN)
    assert store.created == []
