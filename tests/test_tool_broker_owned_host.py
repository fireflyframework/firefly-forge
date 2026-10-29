# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled native-unit broker integration; real network and SQL are not used."""

import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from firefly_forge import tool_broker as module
from firefly_forge.agent_roles import role_digest
from firefly_forge.journal import digest
from firefly_forge.project_access import ProjectAccess
from firefly_forge.runner_factory_authority import RunnerFactoryAuthority
from firefly_forge.runner_pool_models import FactoryFence
from firefly_forge.work_duration_models import WorkDurationPolicyV2

ROOT, REPO = "a" * 32, "b" * 32
SOURCE = {"base_revision": "a" * 40, "head": "a" * 40, "source_tree": "b" * 40}


async def seed(tmp_path, monkeypatch, *, selected=True, chosen=None):
    lock = asyncio.Lock()
    state = {"valid": True, "rows": [], "wal": [], "owned": True}
    token = object()
    key = uuid5(NAMESPACE_URL, f"forge:{ROOT}:implement:1").hex

    @asynccontextmanager
    async def unit():
        async with lock:
            yield SimpleNamespace(scalar=scalar)

    async def scalar(statement):
        return datetime.now(UTC)

    async def owned():
        if not state["owned"]:
            raise RuntimeError("owner lost")

    async def permitted(*args):
        if not state["valid"]:
            raise RuntimeError("current permission revoked")

    async def credentials(*args):
        pass

    store = SimpleNamespace(
        path=tmp_path / "factory.db",
        schema="owned_test",
        _ownership_pid=42,
        execution_ownership_token=token,
        assert_ownership=owned,
        _unit=unit,
        admit_credentials=credentials,
    )
    roles = {
        "roles": [
            {"id": "implementer", "enabled": True, "stages": ["implement"], "allowed_tools": ["internet_search"]}
        ],
        "tool_catalog": [{"id": "internet_search", "availability": "available", "providers": ["agentic"]}],
    }
    roles["digest"] = role_digest(roles)
    run = SimpleNamespace(
        id=UUID(ROOT),
        repository_id=UUID(REPO),
        status="developing",
        data={
            "id": ROOT,
            "repository_id": REPO,
            "agent": "agentic",
            "active_task": "implement",
            "attempt": 1,
            "status": "developing",
            "role_snapshot": roles,
            "search_snapshot": {"revision": "c" * 32},
        },
    )
    if selected:
        policy = WorkDurationPolicyV2(timeout_seconds=chosen).model_dump(mode="json")
        run.data.update(
            work_duration=policy, sandbox_snapshot={"configuration": {"work_duration": copy.deepcopy(policy)}}
        )
    task = SimpleNamespace(status="running", attempt=1, data={"idempotency_key": key, "intent": {}})

    async def current(identity):
        return run

    async def stage(*args):
        return task

    async def append(run_id, task_id, key, kind, payload):
        event = {
            "run_id": run_id,
            "sequence": len(state["wal"]) + 1,
            "task_id": task_id,
            "idempotency_key": key,
            "kind": kind,
            "payload": copy.deepcopy(payload),
            "created_at": "controlled",
            "previous_hash": state["wal"][-1]["digest"] if state["wal"] else "0" * 64,
        }
        event["digest"] = digest(event)
        state["wal"].append(event)
        return event

    await append(ROOT, "implement", key, "intent", {})

    async def task_record(identity, key, kind):
        value = next((x for x in state["wal"] if x["idempotency_key"] == key and x["kind"] == kind), None)
        return SimpleNamespace(**{**value, "run_id": UUID(ROOT), "data": value}) if value else None

    journal = SimpleNamespace(
        _run=current, _task=stage, _append=append, records=SimpleNamespace(task_record=task_record)
    )

    async def rows(identity):
        return state["rows"]

    async def invocation(identity, correlation):
        return next((x for x in state["rows"] if x.correlation_id == correlation), None)

    async def save(row):
        if row not in state["rows"]:
            state["rows"].append(row)

    records = SimpleNamespace(for_run=rows, invocation=invocation, save=save)
    broker = module.ToolInvocationBroker(store, journal, tmp_path / "configuration", records=records)
    broker.configure(module.ToolPolicyInput(timeout_seconds=1))
    run.data["tool_policy_snapshot"] = broker.snapshot()

    async def fence(*args):
        return FactoryFence(factory_schema="owned_test", pid=42, backend_started_at=datetime(2026, 10, 29, tzinfo=UTC))

    monkeypatch.setattr(ProjectAccess, "authorize_execution", permitted)
    monkeypatch.setattr(RunnerFactoryAuthority, "exclusive_fence", fence)
    if hasattr(module, "OWNERSHIP_GRACE_SECONDS"):
        monkeypatch.setattr(module, "OWNERSHIP_GRACE_SECONDS", 1)
    return broker, run, state


async def invoke(broker, run, callback):
    return await broker.invoke(
        ROOT,
        "implement",
        "internet_search",
        "d" * 32,
        {"query": "approved source", "max_results": 1},
        callback,
        source=SOURCE,
        configuration=run.data["search_snapshot"],
    )


async def test_explicit_uncapped_operation_outlives_legacy_budget_and_renews_ownership_once_admitted(
    tmp_path, monkeypatch
):
    broker, run, state = await seed(tmp_path, monkeypatch)
    calls = []

    async def work():
        calls.append(1)
        await asyncio.sleep(1.15)
        return {"results": []}

    result = await invoke(broker, run, work)
    row = state["rows"][0]
    assert calls == [1] and row.status == "succeeded"
    assert row.data["admission"]["operation"]["work_duration"]["timeout_seconds"] is None
    assert row.data["ownership"]["sequence"] >= 2
    assert await invoke(broker, run, work) == result and calls == [1]
    assert [x["kind"] for x in state["wal"]] == ["intent", "tool_intent", "tool_receipt"]


async def test_legacy_absence_keeps_exact_finite_budget_and_no_new_owner_metadata(tmp_path, monkeypatch):
    broker, run, state = await seed(tmp_path, monkeypatch, selected=False)

    async def work():
        return {"results": []}

    await invoke(broker, run, work)
    row = state["rows"][0]
    assert "operation" not in row.data["admission"] and "ownership" not in row.data
    assert row.status == "succeeded"


async def test_chosen_work_deadline_cancels_one_callback_and_keeps_unknown_spending(tmp_path, monkeypatch):
    broker, run, state = await seed(tmp_path, monkeypatch, chosen=1)
    calls = []

    async def work():
        calls.append("entered")
        try:
            await asyncio.Event().wait()
        finally:
            calls.append("drained")

    with pytest.raises(module.ToolReconciliationRequired):
        await invoke(broker, run, work)
    assert calls == ["entered", "drained"] and len(state["rows"]) == 1
    row = state["rows"][0]
    assert row.status == "unknown" and row.data["error"]["operation_outcome"] == "timed_out"
    with pytest.raises(module.ToolReconciliationRequired):
        await invoke(broker, run, work)
    assert calls == ["entered", "drained"]


@pytest.mark.parametrize("lost", ["owner", "permission", "policy"])
async def test_late_output_after_current_authority_changes_cannot_be_admitted_as_success(tmp_path, monkeypatch, lost):
    broker, run, state = await seed(tmp_path, monkeypatch)

    async def work():
        if lost == "owner":
            state["owned"] = False
        elif lost == "permission":
            state["valid"] = False
        else:
            run.data["work_duration"]["timeout_seconds"] = 1
        return {"results": [{"untrusted": "late"}]}

    with pytest.raises(module.ToolReconciliationRequired):
        await invoke(broker, run, work)
    assert state["rows"][0].status != "succeeded"
    assert not any(x["kind"] == "tool_receipt" and x["payload"]["status"] == "succeeded" for x in state["wal"])


async def test_loss_of_owner_heartbeat_drains_one_live_callback_without_success(tmp_path, monkeypatch):
    broker, run, state = await seed(tmp_path, monkeypatch)
    entered, drained = asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    task = asyncio.create_task(invoke(broker, run, work))
    await entered.wait()
    state["owned"] = False
    with pytest.raises(module.ToolReconciliationRequired):
        await asyncio.wait_for(task, 2)
    assert drained.is_set() and state["rows"][0].status != "succeeded"


async def test_live_renewed_operation_cannot_be_abandoned_after_legacy_timeout_and_status_does_not_renew(
    tmp_path, monkeypatch
):
    broker, run, state = await seed(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        await release.wait()
        return {"results": []}

    task = asyncio.create_task(invoke(broker, run, work))
    await entered.wait()
    await asyncio.sleep(1.05)
    try:
        before = copy.deepcopy(state["rows"][0].data)
        with pytest.raises(ValueError, match="still be running"):
            await broker.reconcile(ROOT, "d" * 32, "e" * 32)
        assert state["rows"][0].data == before
    finally:
        release.set()
        await task


async def test_new_operation_cancellation_records_typed_unknown_without_dispatch_retry(tmp_path, monkeypatch):
    broker, run, state = await seed(tmp_path, monkeypatch)
    entered, drained = asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    task = asyncio.create_task(invoke(broker, run, work))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = state["rows"][0]
    assert drained.is_set() and row.status == "unknown" and row.data["error"]["operation_outcome"] == "cancelled"
    with pytest.raises(module.ToolReconciliationRequired):
        await invoke(broker, run, work)


async def test_known_rejection_is_retained_once_with_current_owned_authority(tmp_path, monkeypatch):
    broker, run, state = await seed(tmp_path, monkeypatch)
    calls = []

    async def work():
        calls.append(1)
        raise module.SearchProviderError("brave", "quota", "quota exhausted", status=429)

    with pytest.raises(module.SearchProviderError, match="quota exhausted"):
        await invoke(broker, run, work)
    assert calls == [1] and state["rows"][0].status == "rejected"
    assert [x["kind"] for x in state["wal"]] == ["intent", "tool_intent", "tool_receipt"]


async def test_rejection_after_policy_drift_is_unknown_instead_of_unadmitted_raw_error(tmp_path, monkeypatch):
    broker, run, state = await seed(tmp_path, monkeypatch)

    async def work():
        run.data["work_duration"]["timeout_seconds"] = 1
        raise module.SearchProviderError("brave", "quota", "quota exhausted", status=429)

    with pytest.raises(module.ToolReconciliationRequired):
        await invoke(broker, run, work)
    row = state["rows"][0]
    assert row.status == "unknown" and row.data["error"]["operation_outcome"] == "unavailable"
    assert not any(x["kind"] == "tool_receipt" for x in state["wal"])


async def test_unadmitted_provider_error_retains_owned_unavailable_outcome(tmp_path, monkeypatch):
    broker, run, state = await seed(tmp_path, monkeypatch)

    async def work():
        raise module.SearchProviderError("brave", "transport", "response unavailable")

    with pytest.raises(module.ToolReconciliationRequired):
        await invoke(broker, run, work)
    assert state["rows"][0].data["error"]["operation_outcome"] == "unavailable"
