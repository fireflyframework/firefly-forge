# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Pre-admission epoch failures leave one native refusal and no runnable handoff."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from test_bridge_inflight_cancel import RUN, MemoryStore, Rows

from firefly_forge.journal import Journal, digest
from firefly_forge.verification_handoffs import VerificationHandoffs, automatic_candidate


@pytest.fixture
def refusal_case(tmp_path):
    store = MemoryStore(tmp_path / "store")
    store.run.status = "needs_reconciliation"
    store.run.data.update(
        id=RUN,
        status="needs_reconciliation",
        worker_request={
            "id": "retained-verification",
            "kind": "verification",
            "status": "answered",
            "task_id": "implement",
            "attempt": 1,
            "response_source": "factory_verification",
        },
        verification_handoff={"id": "retained-handoff", "request_id": "retained-verification", "status": "completed"},
    )
    journal = Journal(store, plans=Rows(), tasks=Rows(), records=Rows())
    engine = SimpleNamespace(publication_lock=asyncio.Lock(), assert_ownership=store.assert_ownership)
    return store, journal, VerificationHandoffs(store, journal, engine)


async def test_pre_entry_epoch_refusal_is_persisted_once_and_stops_dispatch(refusal_case, monkeypatch):
    store, journal, service = refusal_case
    entered = []

    @asynccontextmanager
    async def refused(*args):
        entered.append("epoch")
        raise ValueError("Current prepared epoch differs")
        yield

    async def effects(*args):
        pytest.fail("Pre-admission refusal must not execute commands or resume")

    monkeypatch.setattr("firefly_forge.checkpoint_mode_runtime.mode_execution_scope", refused)
    monkeypatch.setattr(service, "_execute", effects)
    original_request = dict(store.run.data["worker_request"])
    original_handoff = dict(store.run.data["verification_handoff"])
    assert automatic_candidate(await store.get_run(RUN))
    await service.execute(RUN)
    assert not automatic_candidate(await store.get_run(RUN))
    refusal = store.run.data["verification_refusal"]
    assert refusal["status"] == "held" and refusal["reason"] == "admission_refused"
    assert refusal["request_digest"] == digest(original_request)
    assert store.run.data["worker_request"] == original_request
    assert store.run.data["verification_handoff"] == original_handoff
    assert store.run.status == "needs_reconciliation"
    await journal.verification_refuse(RUN, digest(original_request))
    assert len(journal.records.rows) == 1
    assert entered == ["epoch"]


async def test_native_continuity_fence_precedes_verification_scope_and_effects(refusal_case, monkeypatch):
    store, _, service = refusal_case
    store.run.data["invalid_read_continuity"] = {"request_id": "a" * 32}

    @asynccontextmanager
    async def scope(*args):
        pytest.fail("Native continuity fence must precede epoch preparation")
        yield

    monkeypatch.setattr("firefly_forge.checkpoint_mode_runtime.mode_execution_scope", scope)
    await service.execute(RUN)
    assert not automatic_candidate(await store.get_run(RUN))


@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError])
async def test_cancellation_or_lost_owner_cannot_write_a_refusal(refusal_case, monkeypatch, failure):
    _, journal, service = refusal_case

    @asynccontextmanager
    async def refused(*args):
        raise failure("Controlled refusal")
        yield

    monkeypatch.setattr("firefly_forge.checkpoint_mode_runtime.mode_execution_scope", refused)
    with pytest.raises(failure):
        await service.execute(RUN)
    assert journal.records.rows == []


async def test_refusal_cannot_bind_different_or_uncompleted_answered_request(refusal_case):
    store, journal, _ = refusal_case
    request = store.run.data["worker_request"]
    await journal.verification_refuse(RUN, "0" * 64)
    assert journal.records.rows == []
    store.run.data["verification_handoff"]["request_id"] = "other"
    await journal.verification_refuse(RUN, digest(request))
    assert journal.records.rows == []
    store.run.data["verification_handoff"].update(request_id=request["id"], status="running")
    await journal.verification_refuse(RUN, digest(request))
    assert journal.records.rows == []


async def test_post_admission_failure_is_not_relabelled_as_pre_admission_refusal(refusal_case, monkeypatch):
    _, journal, service = refusal_case

    @asynccontextmanager
    async def admitted(*args):
        yield

    async def effect_failure(*args):
        raise ValueError("Controlled post-admission failure")

    monkeypatch.setattr("firefly_forge.checkpoint_mode_runtime.mode_execution_scope", admitted)
    monkeypatch.setattr(service, "_execute", effect_failure)
    with pytest.raises(ValueError, match="post-admission"):
        await service.execute(RUN)
    assert journal.records.rows == []
