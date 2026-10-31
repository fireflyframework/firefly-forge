# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Stage observations follow actual Engine execution, never retained running status."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from firefly_forge.engine import RETAINED_EXECUTION_FENCES, Engine
from firefly_forge.engine_activity import EngineActivity, EngineActivitySettings
from firefly_forge.engine_activity_persistence import ForgeEngineActivityRepository
from firefly_forge.project_access import ProjectAccess


@pytest.mark.asyncio
async def test_engine_begins_only_a_new_native_stage_and_ends_after_receipt(monkeypatch, tmp_path):
    engine = Engine.__new__(Engine)
    engine.assert_ownership = AsyncMock()
    engine.store = SimpleNamespace(get_run=AsyncMock(return_value={}))
    engine.journal = SimpleNamespace(
        ensure_plan=AsyncMock(return_value={"tasks": [{"id": "implement", "attempt": 1, "status": "queued"}]}),
        intent=AsyncMock(return_value={"disposition": "execute", "idempotency_key": "a" * 32}),
        receipt=AsyncMock(),
    )
    engine.stage_activity = SimpleNamespace(begin=AsyncMock(), end=AsyncMock())
    monkeypatch.setattr(ProjectAccess, "authorize_execution", AsyncMock())
    await engine._begin_task("b" * 32, "implement", tmp_path, {})
    engine.stage_activity.begin.assert_awaited_once_with("b" * 32, "implement", "a" * 32)
    await engine._receipt("b" * 32, "a" * 32, {})
    engine.stage_activity.end.assert_awaited_once_with("b" * 32, "a" * 32)
    engine.journal.intent.return_value["disposition"] = "completed"
    await engine._begin_task("b" * 32, "implement", tmp_path, {})
    assert engine.stage_activity.begin.await_count == 1


class ObservationStore:
    schema = "forge_observer_test"
    _ownership_pid = 123
    execution_stopping = False

    def __init__(self):
        self.execution_ownership_token = object()
        self.run = {
            "repository_id": uuid4().hex,
            "execution_actor": {"authentication_method": "oidc_bearer", "user_id": uuid4().hex},
        }
        self.stage = SimpleNamespace(data={"idempotency_key": "a" * 32}, plan_id=uuid4(), attempt=1)
        self.session = SimpleNamespace(
            scalar=AsyncMock(
                side_effect=[
                    self.stage,
                    SimpleNamespace(digest="c" * 64),
                    SimpleNamespace(id=uuid4(), digest="d" * 64),
                    datetime.now(UTC),
                ]
            )
        )
        self.units = 0
        self.failure = False

    async def assert_ownership(self):
        if self.failure:
            raise RuntimeError("owner lost")

    async def get_run(self, run_id):
        return self.run

    @asynccontextmanager
    async def _unit(self):
        self.units += 1
        yield self.session


def observer():
    store = ObservationStore()
    records = SimpleNamespace(begin=AsyncMock(return_value=uuid4()), renew=AsyncMock(), end=AsyncMock())
    activity = EngineActivity(store, records, EngineActivitySettings(heartbeat_seconds=0.01, stale_seconds=1))
    return store, records, activity


@pytest.mark.asyncio
async def test_blocked_real_engine_stage_is_observed_then_receipt_ends_it(monkeypatch, tmp_path):
    store, records, activity = observer()
    engine = Engine.__new__(Engine)
    engine.store, engine.stage_activity = store, activity
    engine.assert_ownership = store.assert_ownership
    engine.journal = SimpleNamespace(
        ensure_plan=AsyncMock(return_value={"tasks": [{"id": "implement", "attempt": 1, "status": "queued"}]}),
        intent=AsyncMock(return_value={"disposition": "execute", "idempotency_key": "a" * 32}),
        receipt=AsyncMock(),
    )
    monkeypatch.setattr(ProjectAccess, "authorize_execution", AsyncMock())
    entered, release = asyncio.Event(), asyncio.Event()

    async def stage():
        await engine._begin_task("b" * 32, "implement", tmp_path, {})
        entered.set()
        await release.wait()
        await engine._receipt("b" * 32, "a" * 32, {})

    task = asyncio.create_task(stage())
    try:
        await entered.wait()
        assert len(activity.scopes) == 1 and not task.done()
        assert next(iter(activity.scopes.values())).task is task
        records.renew.return_value = [next(iter(activity.scopes.values())).row_id]
        units = store.units
        await activity.pulse()
        assert store.units == units + 1
        records.renew.assert_awaited_once()
        release.set()
        await task
        assert not activity.scopes
        records.end.assert_awaited_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await activity.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["owner", "backend", "expired", "done", "cancelled"])
async def test_observation_failure_never_replays_or_cancels_business_work(failure):
    store, records, activity = observer()
    entered = asyncio.Event()

    async def business():
        await activity.begin("b" * 32, "implement", "a" * 32)
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(business())
    try:
        await entered.wait()
        assert activity.scopes
        if failure == "owner":
            store.failure = True
        elif failure == "backend":
            records.renew.side_effect = RuntimeError("unavailable")
        elif failure == "expired":
            records.renew.return_value = []
        else:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await activity.pulse()
        assert not activity.scopes
        if failure not in {"done", "cancelled"}:
            assert not task.done()
        records.begin.assert_awaited_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await activity.close()


@pytest.mark.asyncio
async def test_heartbeat_uses_empty_context_and_loses_changed_physical_owner():
    store, records, activity = observer()
    marker = ContextVar("request_identity", default=None)
    marker.set("must_not_leak")
    observed = asyncio.Event()

    async def renew(*args):
        assert marker.get() is None
        observed.set()
        return []

    records.renew.side_effect = renew
    try:
        await activity.begin("b" * 32, "implement", "a" * 32)
        await asyncio.wait_for(observed.wait(), 1)
        assert not activity.scopes
    finally:
        await activity.close()


@pytest.mark.asyncio
async def test_end_during_shutdown_keeps_exact_epoch_and_row_identity():
    store, records, activity = observer()
    await activity.begin("b" * 32, "implement", "a" * 32)
    row = next(iter(activity.scopes.values())).row_id
    store.execution_stopping = True
    await activity.end("b" * 32)
    records.end.assert_awaited_once_with([row], activity.epoch, store.schema)
    await activity.close()


@pytest.mark.parametrize("settings", [{"heartbeat_seconds": 0}, {"stale_seconds": 5}, {"stale_seconds": float("inf")}])
def test_staleness_is_a_validated_observation_interval(settings):
    with pytest.raises(ValidationError):
        EngineActivitySettings(**settings)


@pytest.mark.asyncio
async def test_native_queries_keep_exact_correlation_fencing_and_never_revive_expiry(monkeypatch):
    captured = []
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None), scalars=AsyncMock(return_value=[]), execute=AsyncMock()
    )
    records = ForgeEngineActivityRepository(session=session)
    monkeypatch.setattr(records, "_require_session", lambda: session)
    for method in (session.scalar, session.scalars, session.execute):

        def capture(statement, *args):
            captured.append(str(statement.compile(dialect=postgresql.dialect())))
            return []

        method.side_effect = capture
    await records.renew([uuid4()], "a" * 32, 20, "forge_observer_test")
    await records.end([uuid4()], "a" * 32, "forge_observer_test")
    renewal, end = captured
    assert "forge_engine_activity.expires_at > clock_timestamp()" in renewal
    assert "forge_engine_activity.owner_epoch =" in renewal and "forge_engine_activity.owner_epoch =" in end
    assert "forge_engine_activity.id IN" in end
    assert "FROM pg_catalog.pg_locks, forge_engine_activity" not in renewal
    assert "FROM forge_project_memberships, forge_runs" not in renewal
    assert "FROM forge_plans \nWHERE forge_plans.run_id = forge_runs.id" in renewal
    for required in (
        "forge_tasks.attempt",
        "idempotency_key",
        "forge_journal.digest",
        "api_roles_expires_at",
        "backend_start",
        "pg_locks",
        "scope = jsonb_build_object",
    ):
        assert required in renewal


@pytest.mark.asyncio
async def test_bulk_heartbeat_is_one_unit_for_multiple_current_local_tasks():
    from firefly_forge.engine_activity import LocalStage

    store, records, activity = observer()
    task = asyncio.current_task()
    assert task is not None
    ids = [uuid4(), uuid4()]
    for index, row in enumerate(ids):
        run_id, key = f"{index + 1:032x}", f"{index + 3:032x}"
        activity.scopes[(run_id, key)] = LocalStage(row, run_id, key, task, store.execution_ownership_token)
    records.renew.return_value = ids
    await activity.pulse()
    assert store.units == 1 and len(activity.scopes) == 2
    records.renew.assert_awaited_once_with(ids, activity.epoch, 1, store.schema)
    await activity.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [None, "scm_mapping", "personal_token", "unknown"])
async def test_unsupported_actor_paths_do_not_claim_live_observations(actor):
    store, records, activity = observer()
    store.run["execution_actor"] = {"authentication_method": actor} if actor else None
    await activity.begin("b" * 32, "implement", "a" * 32)
    assert not activity.scopes and activity.poller is None
    records.begin.assert_not_awaited()


@pytest.mark.asyncio
async def test_slow_observation_times_out_without_cancelling_the_engine_task():
    store, records, activity = observer()
    entered = asyncio.Event()

    async def stalled(*args):
        entered.set()
        await asyncio.Event().wait()

    records.begin.side_effect = stalled
    activity.settings = EngineActivitySettings(
        heartbeat_seconds=0.1,
        stale_seconds=1,
        sample_timeout_seconds=0.01,
    )
    await activity.begin("b" * 32, "implement", "a" * 32)
    assert entered.is_set() and not activity.scopes
    assert not asyncio.current_task().cancelling()


def cleanup_engine(tmp_path, monkeypatch, observer_failure):
    from firefly_forge import checkpoint_mode_runtime

    state = {"entered": False, "closed": False}
    run = {
        "id": "b" * 32,
        "repository_id": "c" * 32,
        "status": "preparing",
        "repair_budget": 0,
        "checkpoint_mode_adoption": {"fixture": True},
    }

    @asynccontextmanager
    async def credentials(*args):
        state["entered"] = True
        try:
            yield
        finally:
            state["closed"] = True

    async def finish(identifier, status, **fields):
        run.update(status=status, **fields)

    store = SimpleNamespace(
        path=tmp_path / "state",
        runtime_roles=None,
        configuration=object(),
        claim=AsyncMock(return_value=True),
        get_run=AsyncMock(side_effect=lambda _: dict(run)),
        repository=AsyncMock(side_effect=RuntimeError("business failure")),
        finish=AsyncMock(side_effect=finish),
        event=AsyncMock(),
        release_ownership=AsyncMock(),
    )
    activity = SimpleNamespace(
        begin=AsyncMock(), end=AsyncMock(side_effect=observer_failure), close=AsyncMock(side_effect=observer_failure)
    )
    monkeypatch.setattr(checkpoint_mode_runtime, "mode_execution_scope", credentials)
    monkeypatch.setattr(checkpoint_mode_runtime, "execution_settings", lambda *args: {})
    engine = Engine(store, tmp_path / "workspaces", provider_factory=lambda _: None, stage_activity=activity)
    return engine, state, run


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("observer failed"), asyncio.CancelledError("observer cancelled")])
async def test_failing_observer_cannot_skip_actual_engine_credential_cleanup_or_replace_business_outcome(
    tmp_path, monkeypatch, failure
):
    engine, state, run = cleanup_engine(tmp_path, monkeypatch, failure)
    await engine.execute_run(run["id"])
    assert state == {"entered": True, "closed": True}
    assert run["status"] == "failed" and run["error"] == "business failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("observer failed"), asyncio.CancelledError("observer cancelled")])
async def test_actual_engine_cancellation_still_closes_credentials_when_observer_fails(tmp_path, monkeypatch, failure):
    engine, state, run = cleanup_engine(tmp_path, monkeypatch, failure)
    blocked = asyncio.Event()

    async def repository(*args):
        blocked.set()
        await asyncio.Event().wait()

    engine.store.repository.side_effect = repository
    task = asyncio.create_task(engine.execute_run(run["id"]))
    await asyncio.wait_for(blocked.wait(), 1)
    task.cancel("operator requested")
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    assert cancelled.value.args == ("operator requested",)
    assert state == {"entered": True, "closed": True}
    assert run["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("observer failed"), asyncio.CancelledError("observer cancelled")])
async def test_failing_observer_cannot_skip_drained_engine_owner_release(tmp_path, monkeypatch, failure):
    engine, _, _ = cleanup_engine(tmp_path, monkeypatch, failure)
    engine.enforce_ownership = True
    engine.owner_lock.acquire(timeout=0)
    try:
        await engine.stop()
        engine.store.release_ownership.assert_awaited_once()
        assert not engine.owner_lock.is_locked
    finally:
        if engine.owner_lock.is_locked:
            engine.owner_lock.release()


@pytest.mark.asyncio
async def test_cancellation_during_observer_shutdown_releases_drained_owner_and_propagates(tmp_path, monkeypatch):
    engine, _, _ = cleanup_engine(tmp_path, monkeypatch, None)
    engine.enforce_ownership = True
    engine.owner_lock.acquire(timeout=0)
    entered = asyncio.Event()

    async def blocked_close():
        entered.set()
        await asyncio.Event().wait()

    engine.stage_activity.close.side_effect = blocked_close
    task = asyncio.create_task(engine.stop())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel("shutdown requested")
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await task
        assert cancelled.value.args == ("shutdown requested",)
        engine.store.release_ownership.assert_awaited_once()
        assert not engine.owner_lock.is_locked
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if engine.owner_lock.is_locked:
            engine.owner_lock.release()


@pytest.mark.asyncio
async def test_undrained_consumer_still_holds_owner_without_calling_observer_close(tmp_path, monkeypatch):
    engine, _, _ = cleanup_engine(tmp_path, monkeypatch, RuntimeError("observer failed"))
    engine.enforce_ownership = True
    engine.owner_lock.acquire(timeout=0)
    consumer = asyncio.create_task(asyncio.Event().wait())
    engine.consumer_tasks = lambda: [consumer]
    try:
        await engine.stop()
        assert engine.execution_state == "degraded" and engine.owner_lock.is_locked
        engine.store.release_ownership.assert_not_awaited()
        engine.stage_activity.close.assert_not_awaited()
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        engine.owner_lock.release()
        RETAINED_EXECUTION_FENCES.discard(engine.owner_lock)
