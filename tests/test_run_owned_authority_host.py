# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controller renewal binds the original run/stage/actor/WAL, not just factory health."""

import copy
import importlib
import importlib.util
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_runner_pools import invocation, profile

from firefly_forge.runner_factory_models import FactoryPoolSelection, capture_factory_pool
from firefly_forge.runner_pool_models import FactoryFence
from firefly_forge.runner_run_liveness import RunLivenessBindingV1, run_cut_digest, run_digest, run_task_key
from firefly_forge.work_duration_models import WorkDurationPolicyV2


def case(*, lease_seconds=30):
    duration = WorkDurationPolicyV2(timeout_seconds=864000)
    pool = profile(work_duration=duration, run_liveness="forge-run-owned-liveness-v1", lease_seconds=lease_seconds)
    snapshot = capture_factory_pool(
        FactoryPoolSelection(profile=pool, image=pool.approved_images[0], work_duration=duration)
    )
    fence = FactoryFence(factory_schema="owned_factory", pid=123, backend_started_at=datetime(2026, 10, 28, tzinfo=UTC))
    run = {
        "id": "a" * 32,
        "repository_id": "b" * 32,
        "project_id": None,
        "agent": "bridge",
        "publish": False,
        "status": "validating",
        "active_task": "verify",
        "execution_actor": {"user_id": "c" * 32},
        "sandbox_snapshot": snapshot,
    }
    binding = RunLivenessBindingV1(
        task_id="verify",
        attempt=2,
        snapshot_digest=snapshot["digest"],
        actor_digest=run_digest(run["execution_actor"]),
        run_cut_digest=run_cut_digest(run),
        factory_fence_digest=run_digest(fence.model_dump(mode="json")),
        worker_epoch="d" * 32,
    )
    request = invocation(
        pool, repository=UUID(run["repository_id"]), run_liveness=binding, timeout=864000, work_duration=duration
    )
    request = request.model_copy(update={"run_id": UUID(run["id"])})
    payload = {
        "parent_attempt": 2,
        "ordinal": 1,
        "snapshot_digest": snapshot["digest"],
        "factory_fence": fence.model_dump(mode="json"),
        "request_digest": run_digest(request.model_dump(mode="json")),
        "request": request.model_dump(mode="json"),
        "command": {
            "argv": list(request.argv),
            "cwd": request.cwd,
            "timeout": request.timeout,
            "snapshot_digest": snapshot["digest"],
        },
    }
    wal = {
        "execution_id": request.execution_id,
        "run_id": run["id"],
        "task_id": "verify",
        "parent_attempt": 2,
        "ordinal": 1,
        "status": "running",
        "snapshot_digest": snapshot["digest"],
        "request": payload,
        "effects": {"start": {"status": "intent"}},
    }
    task = {
        "task_id": "verify",
        "status": "running",
        "attempt": 2,
        "idempotency_key": run_task_key(run["id"], "verify", 2),
    }
    return request, fence, run, task, wal


def validator():
    assert importlib.util.find_spec("firefly_forge.runner_run_authority") is not None, (
        "Controller run authority is missing"
    )
    return importlib.import_module("firefly_forge.runner_run_authority").validate_run_authority


def test_current_exact_run_stage_and_wal_are_admitted_without_a_work_age_test():
    validator()(*case())


@pytest.mark.parametrize(
    "mutation",
    [
        "cancel",
        "run_status",
        "actor",
        "stage",
        "attempt",
        "snapshot",
        "fence",
        "run",
        "request",
        "ordinal",
        "command",
        "completed",
        "cut",
    ],
)
def test_healthy_factory_cannot_renew_a_revoked_foreign_or_changed_run(mutation):
    request, fence, run, task, wal = copy.deepcopy(case())
    if mutation == "cancel":
        run["cancel_request"] = {"status": "requested"}
    elif mutation == "run_status":
        run["status"] = "cancelled"
    elif mutation == "actor":
        run["execution_actor"]["user_id"] = "e" * 32
    elif mutation == "stage":
        task["status"] = "done"
    elif mutation == "attempt":
        task["attempt"] = 3
    elif mutation == "snapshot":
        wal["snapshot_digest"] = "f" * 64
    elif mutation == "fence":
        fence = fence.model_copy(update={"pid": 124})
    elif mutation == "run":
        wal["run_id"] = "e" * 32
    elif mutation == "request":
        wal["request"]["request"]["argv"] = ["changed"]
    elif mutation == "ordinal":
        wal["ordinal"] = 2
    elif mutation == "command":
        wal["request"]["command"]["timeout"] = 12
    elif mutation == "completed":
        wal["effects"]["start"]["status"] = "done"
    else:
        run["publish"] = True
    with pytest.raises(ValueError):
        validator()(request, fence, run, task, wal)


@pytest.mark.parametrize("failure", [None, "permission", "journal", "late_cancel"])
async def test_controller_reader_rechecks_native_permission_and_original_start_intent(monkeypatch, failure):
    from firefly_forge import persistence, sandbox_persistence
    from firefly_forge.journal import ZERO_HASH, digest
    from firefly_forge.project_access import AccessDenied, ProjectAccess

    implementation = importlib.import_module("firefly_forge.runner_run_authority")
    assert hasattr(implementation, "RunnerRunAuthority"), "Controller factory reader is missing"
    request, fence, run, task, wal = case()
    row = SimpleNamespace(id=request.run_id, data=run, status=run["status"])
    stage = SimpleNamespace(**task, data={"idempotency_key": task["idempotency_key"]})
    native = SimpleNamespace(
        id=UUID(wal["execution_id"]),
        run_id=request.run_id,
        task_id=wal["task_id"],
        parent_attempt=2,
        ordinal=1,
        container_name="pool-" + request.execution_id,
        status="running",
        snapshot_digest=wal["snapshot_digest"],
        data={"request": wal["request"], "effects": wal["effects"]},
    )
    event = {
        "run_id": run["id"],
        "sequence": 1,
        "previous_hash": ZERO_HASH,
        "task_id": "verify",
        "kind": "sandbox_intent",
        "payload": {"execution_id": request.execution_id, "effect": "start", "request": wal["request"]},
    }
    chain = [SimpleNamespace(data=event, digest=digest(event) if failure != "journal" else "0" * 64)]
    permissions = []

    async def read_run(self, key, **kwargs):
        return row

    async def read_wal(self, key, **kwargs):
        return native

    async def tasks(self, key):
        return [stage]

    async def history(self, key):
        return chain

    async def repository(self, key):
        return SimpleNamespace(id=key)

    async def execution(self, value, permission="execute"):
        permissions.append(permission)
        if failure == "permission":
            raise AccessDenied("Controlled current permission revoked")
        if failure == "late_cancel":
            row.data["cancel_request"] = {"status": "requested"}

    async def actor(self, *args):
        return True

    @asynccontextmanager
    async def unit():
        yield object()

    monkeypatch.setattr(persistence.ForgeRunRepository, "find_by_id", read_run)
    monkeypatch.setattr(sandbox_persistence.ForgeSandboxRepository, "find_by_id", read_wal)
    monkeypatch.setattr(persistence.ForgeTaskRepository, "find_for_run", tasks)
    monkeypatch.setattr(persistence.ForgeJournalRepository, "find_for_run", history)
    monkeypatch.setattr(persistence.ForgeRepositoryRepository, "find_by_id", repository)
    monkeypatch.setattr(ProjectAccess, "authorize_execution", execution)
    monkeypatch.setattr(ProjectAccess, "authorize_actor", actor)
    authority = implementation.RunnerRunAuthority(SimpleNamespace(factory_schema="owned_factory"))
    monkeypatch.setattr(authority, "_unit", unit)
    if failure is None:
        await authority.authorize(request, fence)
        assert permissions == ["execute"]
    else:
        with pytest.raises((ValueError, AccessDenied)):
            await authority.authorize(request, fence)
        if failure == "journal":
            assert permissions == []


@pytest.mark.parametrize("stage,status", [("implement", "developing"), ("verify", "validating")])
def test_controller_authority_uses_actual_engine_stage_state_during_child_wait(stage, status):
    request, fence, run, task, wal = case()
    binding = request.run_liveness.model_copy(update={"task_id": stage})
    request = request.model_copy(update={"run_liveness": binding})
    run.update(status=status, active_task=stage, factory_wait={"version": 1, "kind": "children"})
    task["task_id"] = wal["task_id"] = stage
    task["idempotency_key"] = run_task_key(run["id"], stage, 2)
    wal["request"]["request"] = request.model_dump(mode="json")
    wal["request"]["request_digest"] = run_digest(request.model_dump(mode="json"))
    validator()(request, fence, run, task, wal)
    for wrong in ("running", "paused", "developing" if stage == "verify" else "validating"):
        with pytest.raises(ValueError):
            validator()(request, fence, {**run, "status": wrong}, task, wal)
    with pytest.raises(ValueError):
        validator()(request, fence, {**run, "active_task": "other"}, task, wal)


@pytest.mark.parametrize("stage,status", [("implement", "developing"), ("verify", "validating")])
async def test_worker_factory_authority_accepts_exact_engine_stage_for_selected_protocol(monkeypatch, stage, status):
    from firefly_forge.project_access import ProjectAccess
    from firefly_forge.runner_factory_authority import RunnerFactoryAuthority

    _, fence, run, _, _ = case()
    run.update(status=status, active_task=stage)

    async def ownership():
        pass

    async def get_run(identity):
        return run

    async def repository(identity):
        return {"id": identity}

    async def plan(identity):
        return {
            "tasks": [
                {"id": stage, "status": "running", "attempt": 2, "idempotency_key": run_task_key(run["id"], stage, 2)}
            ]
        }

    async def permitted(*args):
        return True

    store = SimpleNamespace(assert_ownership=ownership, get_run=get_run, repository=repository)
    authority = RunnerFactoryAuthority(store, SimpleNamespace(plan=plan))
    monkeypatch.setattr(ProjectAccess, "authorize_execution", permitted)
    monkeypatch.setattr(ProjectAccess, "authorize_actor", permitted)

    async def exclusive():
        return fence

    monkeypatch.setattr(authority, "exclusive_fence", exclusive)
    assert (await authority.authorize(run["id"], stage))["attempt"] == 2
    run["active_task"] = "other"
    with pytest.raises(RuntimeError):
        await authority.authorize(run["id"], stage)


def test_task_key_cannot_be_replaced_with_another_stage_intent_at_the_same_attempt():
    request, fence, run, task, wal = case()
    task["idempotency_key"] = "f" * 32
    with pytest.raises(ValueError):
        validator()(request, fence, run, task, wal)


def test_run_top_level_work_policy_change_revokes_the_original_owner():
    request, fence, run, task, wal = case()
    run["work_duration"] = {"protocol": "forge-work-duration-v2", "timeout_seconds": 12}
    with pytest.raises(ValueError):
        validator()(request, fence, run, task, wal)


@pytest.mark.parametrize(
    "changed", ["ordinal_bool", "stdin_bool", "command_float", "fence_float", "attempt_float", "wal_attempt_float"]
)
def test_raw_wal_json_cannot_coerce_values_while_retaining_the_original_digest(changed):
    request, fence, run, task, wal = copy.deepcopy(case())
    payload = wal["request"]
    if changed == "ordinal_bool":
        payload["ordinal"] = True
    elif changed == "stdin_bool":
        payload["request"]["stdin_bytes"] = False
    elif changed == "command_float":
        payload["command"]["timeout"] = float(request.timeout)
    elif changed == "fence_float":
        payload["factory_fence"]["pid"] = float(fence.pid)
    elif changed == "attempt_float":
        payload["parent_attempt"] = float(payload["parent_attempt"])
    else:
        wal["parent_attempt"] = float(wal["parent_attempt"])
    with pytest.raises(ValueError):
        validator()(request, fence, run, task, wal)
