# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native child claims and bounded observations release and reacquire parent compute."""

import asyncio

import pytest
from test_factory_delegation_dispatch_host import prepared

from firefly_forge.delegation_models import DelegationWaitInput


async def test_real_coordinator_dispatches_once_then_durably_waits_and_reacquires(tmp_path, monkeypatch):
    manager, session, native, child, _ = await prepared(tmp_path, monkeypatch)
    assert hasattr(manager, "runtime"), "Missing concrete factory child scheduler"
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def worker(run):
        calls.append(run["id"])
        entered.set()
        await release.wait()
        return {"summary": "Independent analysis", "changes": [], "delivery_verified": False}

    manager.runtime.worker = worker
    scope = await manager.runtime.scheduling_scope()
    assert scope.ready_children == (child["id"],)
    request = DelegationWaitInput(request_id="1" * 32, delegation_ids=[child["id"]])
    waiting = asyncio.create_task(session.wait(request))
    await manager.runtime.changed.wait()
    await asyncio.sleep(0)
    assert session.run_id in (await manager.runtime.scheduling_scope()).waiting_parents
    execution = asyncio.create_task(manager.runtime.execute_child(child["id"]))
    await entered.wait()
    release.set()
    await execution
    while next(iter(manager.runtime.waits.values())).stage != "reacquiring":
        await asyncio.sleep(0)
    await manager.runtime.reacquire(0)
    assert not waiting.done(), "A completed child cannot oversubscribe parent compute"
    assert await manager.runtime.reacquire(1) == 1
    outcome = await asyncio.wait_for(waiting, 2)
    assert outcome["outcome"] == "satisfied" and outcome["children"][0]["state"] == "completed"
    assert await session.wait(request) == outcome
    assert calls == [child["id"]]
    with pytest.raises(ValueError):
        await manager.runtime.execute_child(child["id"])
    kinds = [row["payload"]["event"]["kind"] for row in native.rows if row["kind"] == "factory_team_event"]
    assert kinds == [
        "child_admitted",
        "wait_started",
        "child_claimed",
        "child_completed",
        "wait_result",
        "wait_resumed",
    ]


async def test_missing_worker_refuses_before_claim_and_interrupted_callback_never_replays(tmp_path, monkeypatch):
    manager, session, native, child, _ = await prepared(tmp_path, monkeypatch)
    assert hasattr(manager, "runtime"), "Missing concrete factory child scheduler"
    with pytest.raises(ValueError, match="worker"):
        await manager.runtime.execute_child(child["id"])
    assert len(native.rows) == 2
    calls = 0

    async def unknown(run):
        nonlocal calls
        calls += 1
        raise RuntimeError("Controlled provider interruption")

    manager.runtime.worker = unknown
    await manager.runtime.execute_child(child["id"])
    observed = await manager.observe(session, [child["id"]])
    assert observed["children"][0]["state"] == "needs_reconciliation"
    with pytest.raises(ValueError):
        await manager.runtime.execute_child(child["id"])
    assert calls == 1


async def test_new_instance_reads_known_wait_but_cannot_adopt_pending_wait(tmp_path, monkeypatch):
    manager, session, native, child, _ = await prepared(tmp_path, monkeypatch)
    assert hasattr(manager, "runtime"), "Missing concrete factory child scheduler"
    request = DelegationWaitInput(request_id="2" * 32, delegation_ids=[child["id"]])
    pending = asyncio.create_task(session.wait(request))
    await manager.runtime.changed.wait()
    await asyncio.sleep(0)
    from firefly_forge.delegation_runtime import DelegationRuntime

    restarted = DelegationRuntime(manager)
    restarted.register(session)
    observed = await restarted.wait_status(session, request.request_id)
    assert observed["outcome"] == "pending"
    with pytest.raises(ValueError, match="adopt"):
        await restarted.wait(session, request)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert (await restarted.wait_status(session, request.request_id))["outcome"] == "cancelled"
    assert manager.runtime.bus.listener_count() == 0


async def test_native_current_authority_is_rechecked_while_waiting_without_work_timeout(tmp_path, monkeypatch):
    manager, session, native, child, _ = await prepared(tmp_path, monkeypatch)
    assert hasattr(manager, "runtime"), "Missing concrete factory child scheduler"
    request = DelegationWaitInput(request_id="3" * 32, delegation_ids=[child["id"]])
    waiting = asyncio.create_task(session.wait(request))
    await manager.runtime.changed.wait()
    native.run["cancel_request"] = {"status": "requested"}
    await manager.runtime.notify()
    with pytest.raises(ValueError, match="current parent"):
        await asyncio.wait_for(waiting, 2)
    assert manager.runtime.bus.listener_count() == 0
    assert not (await manager.runtime.scheduling_scope()).waiting_parents


async def test_authority_loss_cancels_the_running_child_without_publishing_success(tmp_path, monkeypatch):
    manager, session, native, child, _ = await prepared(tmp_path, monkeypatch)
    entered, unwound = asyncio.Event(), asyncio.Event()

    async def worker(run):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            unwound.set()

    manager.runtime.worker = worker
    execution = asyncio.create_task(manager.runtime.execute_child(child["id"]))
    await entered.wait()
    native.run["cancel_request"] = {"status": "requested"}
    await manager.runtime.notify()
    try:
        with pytest.raises(ValueError, match="current parent"):
            await asyncio.wait_for(asyncio.shield(execution), 2)
        assert unwound.is_set()
        kinds = [row["payload"]["event"]["kind"] for row in native.rows if row["kind"] == "factory_team_event"]
        assert "child_completed" not in kinds and "child_claimed" in kinds
    finally:
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


async def test_missing_required_checkpoint_wait_proof_refuses_before_yield(tmp_path, monkeypatch):
    manager, session, native, child, _ = await prepared(tmp_path, monkeypatch)
    from firefly_forge.agentic_checkpoint_invocation import _current

    token = _current.set(object())
    try:
        with pytest.raises(ValueError, match="parked"):
            await asyncio.wait_for(
                session.wait(DelegationWaitInput(request_id="4" * 32, delegation_ids=[child["id"]])), 1
            )
    finally:
        _current.reset(token)
    assert len(native.rows) == 2 and not manager.runtime.waits


async def test_join_persists_deterministic_analysis_result_without_source_application(tmp_path, monkeypatch):
    manager, session, native, child, _ = await prepared(tmp_path, monkeypatch)
    from firefly_forge.delegation_models import DelegationJoinInput

    async def worker(run):
        return {"summary": "Analysis only", "changes": [], "delivery_verified": False}

    manager.runtime.worker = worker
    await manager.runtime.execute_child(child["id"])
    assert hasattr(session, "join"), "Missing durable deterministic fan-in"
    request = DelegationJoinInput(request_id="5" * 32, delegation_ids=[child["id"]])
    result = await session.join(request)
    assert result["status"] == "ready" and not result["applied"] and not result["delivery_verified"]
    assert await session.join(request) == result
    assert result["children"][0]["summary"] == "Analysis only"


async def test_cancel_queued_child_is_durable_and_never_invokes_a_worker(tmp_path, monkeypatch):
    manager, session, native, child, _ = await prepared(tmp_path, monkeypatch)
    from firefly_forge.delegation_models import DelegationCancelInput

    assert hasattr(session, "cancel"), "Missing exact child cancellation"
    request = DelegationCancelInput(request_id="8" * 32, delegation_id=child["id"])
    result = await session.cancel(request)
    assert result["state"] == "cancelled" and result["claim_digest"] is None
    assert await session.cancel(request) == result
    with pytest.raises(ValueError):
        await manager.claim_child(session, child["id"])
    assert len(native.rows) == 4


async def test_cancel_running_child_waits_for_worker_cleanup_before_terminal(tmp_path, monkeypatch):
    manager, session, native, child, _ = await prepared(tmp_path, monkeypatch)
    from firefly_forge.delegation_models import DelegationCancelInput

    assert hasattr(session, "cancel"), "Missing exact child cancellation"
    entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def worker(run):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()

    manager.runtime.worker = worker
    execution = asyncio.create_task(manager.runtime.execute_child(child["id"]))
    await entered.wait()
    cancel = asyncio.create_task(session.cancel(DelegationCancelInput(request_id="9" * 32, delegation_id=child["id"])))
    await asyncio.wait_for(cleanup.wait(), 1)
    assert not cancel.done()
    assert (await manager.observe(session, [child["id"]]))["children"][0]["state"] == "running"
    release.set()
    result = await asyncio.wait_for(cancel, 2)
    assert result["state"] == "cancelled" and result["summary"] == ""
    await asyncio.gather(execution, return_exceptions=True)


async def test_repeat_cancel_cannot_publish_before_cleanup(tmp_path, monkeypatch):
    manager, session, native, child, _ = await prepared(tmp_path, monkeypatch)
    entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def worker(run):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()

    manager.runtime.worker = worker
    execution = asyncio.create_task(manager.runtime.execute_child(child["id"]))
    await entered.wait()
    execution.cancel()
    await cleanup.wait()
    execution.cancel()
    await asyncio.sleep(0.03)
    try:
        assert not execution.done(), "Repeated cancellation published before owned cleanup"
        observed = await manager.observe(session, [child["id"]])
        assert observed["children"][0]["state"] == "running"
    finally:
        release.set()
        await asyncio.gather(execution, return_exceptions=True)


async def test_unconfirmed_drain_remains_owned_and_unknown_until_worker_really_finishes(tmp_path, monkeypatch):
    manager, session, native, child, _ = await prepared(tmp_path, monkeypatch)
    monkeypatch.setattr("firefly_forge.delegation_runtime.CHILD_DRAIN_SECONDS", 0.02)
    entered, release = asyncio.Event(), asyncio.Event()

    async def worker(run):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await release.wait()

    manager.runtime.worker = worker
    execution = asyncio.create_task(manager.execute_child(child["id"]))
    await entered.wait()
    execution.cancel()
    result = await asyncio.wait_for(asyncio.shield(execution), 1)
    try:
        assert result["state"] == "needs_reconciliation"
        assert hasattr(manager.runtime, "workers"), "Unconfirmed owned worker must remain tracked"
        pending = manager.runtime.workers[child["id"]]
        assert not pending.done()
    finally:
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert child["id"] not in manager.runtime.workers
