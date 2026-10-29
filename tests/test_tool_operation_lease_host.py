# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Renewal is a current native authority check, never a new invocation admission."""

import copy
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from firefly_forge.journal import digest
from firefly_forge.project_access import ProjectAccess
from firefly_forge.tool_operation_lease import OwnedToolLease, operation_cut
from firefly_forge.tool_operation_models import OwnedToolOperationV2
from firefly_forge.work_duration_models import WorkDurationPolicyV2

ROOT = "a" * 32
CORRELATION = "b" * 32


async def seed(monkeypatch):
    key = uuid5(NAMESPACE_URL, f"forge:{ROOT}:implement:1").hex
    token = object()
    clock = [datetime(2026, 10, 29, tzinfo=UTC).timestamp()]
    policy = WorkDurationPolicyV2().model_dump(mode="json")
    run = SimpleNamespace(
        id=UUID(ROOT),
        status="developing",
        data={
            "id": ROOT,
            "status": "developing",
            "active_task": "implement",
            "attempt": 1,
            "execution_actor": None,
            "work_duration": policy,
            "sandbox_snapshot": {"configuration": {"work_duration": copy.deepcopy(policy)}},
        },
    )
    task = SimpleNamespace(status="running", attempt=1, data={"idempotency_key": key, "intent": {}})
    body = {
        "run_id": ROOT,
        "sequence": 1,
        "task_id": "implement",
        "idempotency_key": key,
        "kind": "intent",
        "payload": {},
        "created_at": "controlled",
        "previous_hash": "0" * 64,
    }
    body["digest"] = digest(body)
    intent = SimpleNamespace(**{**body, "run_id": UUID(ROOT), "data": body})
    record = OwnedToolOperationV2(
        version=2,
        generation="c" * 32,
        task_key=key,
        intent_digest=intent.digest,
        task_id="implement",
        attempt=1,
        factory_fence={
            "factory_schema": "owned_test",
            "pid": 42,
            "backend_started_at": datetime(2026, 10, 29, tzinfo=UTC),
        },
        run_cut=operation_cut(run.data),
        work_duration=WorkDurationPolicyV2(),
        work_started_at_ms=int(clock[0] * 1000),
        work_deadline_ms=None,
        owner_grace_seconds=30,
    )
    admission = {
        "operation": record.model_dump(mode="json"),
        "run_id": ROOT,
        "task_id": "implement",
        "tool": "internet_search",
        "correlation_id": CORRELATION,
    }
    progress = {"generation": record.generation, "sequence": 0, "lease_expires_at": clock[0] + 30}
    row = SimpleNamespace(
        run_id=UUID(ROOT),
        task_id="implement",
        tool="internet_search",
        correlation_id=CORRELATION,
        status="pending",
        data={
            "admission": admission,
            "request_digest": digest(admission),
            "ownership": copy.deepcopy(progress),
            "lease_expires_at": progress["lease_expires_at"],
        },
    )
    writes = []

    async def scalar(statement):
        return datetime.fromtimestamp(clock[0], UTC)

    @asynccontextmanager
    async def unit():
        yield SimpleNamespace(scalar=scalar)

    async def owned():
        pass

    store = SimpleNamespace(
        schema="owned_test", _ownership_pid=42, execution_ownership_token=token, assert_ownership=owned, _unit=unit
    )

    async def current(run_id):
        return run

    async def stage(*args):
        return task

    async def native_record(*args):
        return intent if args[-1] == "intent" else None

    journal = SimpleNamespace(_run=current, _task=stage, records=SimpleNamespace(task_record=native_record))

    async def invocation(*args):
        return row

    async def save(value):
        writes.append(copy.deepcopy(value.data))

    records = SimpleNamespace(invocation=invocation, save=save)

    async def permitted(*args):
        pass

    monkeypatch.setattr(ProjectAccess, "authorize_execution", permitted)
    lease = OwnedToolLease(store, journal, records, ROOT, CORRELATION, record, token, digest(admission), progress)
    return lease, store, run, task, intent, row, clock, writes


async def test_healthy_current_owner_renews_only_finite_grace_and_keeps_optional_work_deadline(monkeypatch):
    lease, _, _, _, _, row, clock, writes = await seed(monkeypatch)
    clock[0] += 10
    await lease.renew()
    assert len(writes) == 1 and row.data["ownership"]["sequence"] == 1
    assert row.data["lease_expires_at"] == clock[0] + 30
    assert row.data["admission"]["operation"]["work_deadline_ms"] is None
    before = copy.deepcopy(row.data["admission"])
    clock[0] += 10
    await lease.renew()
    assert row.data["admission"] == before and row.data["ownership"]["sequence"] == 2


@pytest.mark.parametrize(
    "case",
    [
        "owner_token",
        "owner_pid",
        "run_cancel",
        "run_phase",
        "task_attempt",
        "task_key",
        "intent_digest",
        "progress_bool",
        "expired",
        "frozen_policy",
        "tool_scalar",
        "task_scalar",
        "correlation_scalar",
    ],
)
async def test_stale_or_replaced_authority_cannot_renew_or_adopt_an_old_pending_operation(monkeypatch, case):
    lease, store, run, task, intent, row, clock, writes = await seed(monkeypatch)
    if case == "owner_token":
        store.execution_ownership_token = object()
    elif case == "owner_pid":
        store._ownership_pid = 43
    elif case == "run_cancel":
        run.data["cancel_request"] = {"status": "requested"}
    elif case == "run_phase":
        run.data["status"] = run.status = "paused"
    elif case == "task_attempt":
        task.attempt = 2
    elif case == "task_key":
        task.data["idempotency_key"] = "f" * 32
    elif case == "intent_digest":
        intent.digest = "f" * 64
    elif case == "progress_bool":
        row.data["ownership"]["sequence"] = False
    elif case == "expired":
        clock[0] += 30
    elif case == "tool_scalar":
        row.tool = "knowledge_lookup"
    elif case == "task_scalar":
        row.task_id = "verify"
    elif case == "correlation_scalar":
        row.correlation_id = "e" * 32
    else:
        run.data["work_duration"]["timeout_seconds"] = 5
    with pytest.raises((ValueError, RuntimeError)):
        await lease.renew()
    assert writes == []
