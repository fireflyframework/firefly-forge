# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""New queued work has a renewable security lease; retained queue expiry stays finite."""

import copy
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_run_owned_authority_host import case

from firefly_forge.runner_factory_models import FactoryPoolSelection
from firefly_forge.runner_pools import RunnerPools
from firefly_forge.runner_run_liveness import RunLease, RunRenewalV1, run_digest


def fixture(monkeypatch):
    invocation, fence, run, *_ = case(work_cap=None)
    profile = FactoryPoolSelection.model_validate(run["sandbox_snapshot"]["configuration"]).profile
    clock = {"elapsed": 0.0}
    origin = datetime(2026, 10, 29, tzinfo=UTC)
    lease = RunLease(
        run_digest(invocation.model_dump(mode="json")),
        invocation.run_liveness,
        "e" * 32,
        30,
        clock=lambda: clock["elapsed"],
    )
    pool = SimpleNamespace(id=uuid4())
    row = SimpleNamespace(
        id=uuid4(),
        pool_id=pool.id,
        execution_id=invocation.execution_id,
        status="queued",
        owner_id=None,
        epoch=0,
        lease_expires_at=None,
        admission_expires_at=origin + timedelta(seconds=30),
        data={
            "request": invocation.model_dump(mode="json"),
            "submission_fence": fence.model_dump(mode="json"),
            "run_admission_protocol": "forge-run-owned-admission-v1",
            "run_admission_owner": lease.ack(),
        },
    )
    events = []

    async def flush():
        pass

    @asynccontextmanager
    async def unit():
        yield SimpleNamespace(flush=flush)

    async def selected(*args, **kwargs):
        return pool

    async def owned(value):
        assert value == fence

    async def execution(identity):
        assert identity == invocation.execution_id
        return row

    async def database_time():
        return origin + timedelta(seconds=clock["elapsed"])

    async def event(*args):
        events.append(args)

    service = RunnerPools(None, "owned_runner", factory_schema="owned_factory")
    monkeypatch.setattr(service, "_unit", unit)
    monkeypatch.setattr(service, "_pool", selected)
    monkeypatch.setattr(service, "_owned", owned)
    monkeypatch.setattr(service, "_event", event)
    service.invocations = SimpleNamespace(execution=execution, database_time=database_time)
    return service, profile, invocation, fence, lease, row, clock, events


def request_and_ack(lease):
    request = RunRenewalV1.model_validate(
        {**lease.ack(), "protocol": "forge-run-renewal-v1", "sequence": lease.sequence + 1}
    )
    return request, lease.renew(request)


async def test_current_queued_owner_can_wait_past_its_first_deadline_without_resetting_identity(monkeypatch):
    service, profile, invocation, fence, lease, row, clock, events = fixture(monkeypatch)
    assert hasattr(service, "renew_run_admission"), "Renewable run-owned queue authority is missing"
    original = copy.deepcopy(row.data["request"])
    for _ in range(100):
        clock["elapsed"] += 20
        request, ack = request_and_ack(lease)
        assert await service.renew_run_admission(profile, invocation, fence, request, ack) is True
        assert row.admission_expires_at == datetime(2026, 10, 29, tzinfo=UTC) + timedelta(seconds=clock["elapsed"] + 30)
        assert row.data["run_admission_owner"] == ack and row.data["request"] == original
        assert row.status == "queued" and row.owner_id is None and row.epoch == 0
    assert clock["elapsed"] > profile.admission_seconds and not events


@pytest.mark.parametrize(
    "changed",
    [
        "expired",
        "cancelled",
        "claimed_then_queued",
        "worker",
        "controller",
        "sequence",
        "challenge",
        "raw_json",
        "fence",
        "unknown_owner",
        "continuation",
    ],
)
async def test_queued_renewal_never_revives_or_rebinds_old_or_foreign_authority(monkeypatch, changed):
    service, profile, invocation, fence, lease, row, clock, events = fixture(monkeypatch)
    assert hasattr(service, "renew_run_admission"), "Renewable run-owned queue authority is missing"
    request, ack = request_and_ack(lease)
    if changed == "expired":
        clock["elapsed"] = 30
    elif changed == "cancelled":
        row.status = "cancelled"
    elif changed == "claimed_then_queued":
        row.epoch = 1
    elif changed == "worker":
        row.data["run_admission_owner"]["worker_epoch"] = "f" * 32
    elif changed == "controller":
        row.data["run_admission_owner"]["controller_epoch"] = "f" * 32
    elif changed == "sequence":
        row.data["run_admission_owner"]["sequence"] = 1
    elif changed == "challenge":
        row.data["run_admission_owner"]["challenge"] = "f" * 32
    elif changed == "raw_json":
        row.data["request"]["stdin_bytes"] = False
    elif changed == "fence":
        row.data["submission_fence"]["pid"] = float(fence.pid)
    elif changed == "unknown_owner":
        row.data.pop("run_admission_owner")
    else:
        row.data["continuation"] = {"unknown": True}
    before = copy.deepcopy(row.__dict__)
    with pytest.raises((ValueError, RuntimeError)):
        await service.renew_run_admission(profile, invocation, fence, request, ack)
    assert row.__dict__ == before and not events


async def test_already_started_work_cannot_extend_its_waiting_deadline(monkeypatch):
    service, profile, invocation, fence, lease, row, clock, events = fixture(monkeypatch)
    assert hasattr(service, "renew_run_admission"), "Renewable run-owned queue authority is missing"
    row.status = "running"
    row.owner_id = lease.controller_epoch
    row.epoch = 1
    row.lease_expires_at = row.admission_expires_at
    row.data["fence"] = fence.model_dump(mode="json")
    request, ack = request_and_ack(lease)
    before = copy.deepcopy(row.__dict__)
    assert await service.renew_run_admission(profile, invocation, fence, request, ack) is False
    assert row.__dict__ == before and not events


async def test_existing_owned_submission_cannot_acquire_another_initial_owner_ack(monkeypatch):
    service, profile, invocation, fence, lease, row, clock, events = fixture(monkeypatch)
    with pytest.raises(RuntimeError, match="reconciliation|Prior"):
        await service.submit(profile, invocation, fence=fence, run_lease=lease.ack())
    assert row.data["run_admission_owner"] == lease.ack() and not events


async def test_submission_fence_must_match_the_original_typed_run_binding_before_db_access(monkeypatch):
    service, profile, invocation, fence, lease, row, clock, events = fixture(monkeypatch)
    with pytest.raises(ValueError, match="fence"):
        await service.submit(
            profile, invocation, fence=fence.model_copy(update={"pid": fence.pid + 1}), run_lease=lease.ack()
        )
    assert not events
