# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Factory commands use actual Engine phases and the original active task intent."""

from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from firefly_forge.project_access import ProjectAccess
from firefly_forge.runner_factory_authority import RunnerFactoryAuthority


async def authority(monkeypatch, *, stage="verify", status="validating", legacy=False):
    identity = "a" * 32
    duration = {"protocol": "forge-work-duration-v2", "timeout_seconds": 864000}
    run = {
        "id": identity,
        "repository_id": "b" * 32,
        "status": status,
        "active_task": stage,
        "sandbox_snapshot": {"configuration": {} if legacy else {"work_duration": duration}},
    }
    task = {
        "id": stage,
        "status": "running",
        "attempt": 2,
        "idempotency_key": uuid5(NAMESPACE_URL, f"forge:{identity}:{stage}:2").hex,
    }
    effects = []

    async def owned():
        effects.append("ownership")

    async def current(key):
        assert key == identity
        return run

    async def repository(key):
        effects.append("repository")

    async def plan(key):
        return {"tasks": [task]}

    async def permitted(*args):
        effects.append("permission")

    async def fence():
        effects.append("fence")
        return "controlled-exact-fence"

    store = SimpleNamespace(assert_ownership=owned, get_run=current, repository=repository)
    service = RunnerFactoryAuthority(store, SimpleNamespace(plan=plan))
    monkeypatch.setattr(ProjectAccess, "authorize_execution", permitted)
    monkeypatch.setattr(service, "exclusive_fence", fence)
    return service, run, task, effects


@pytest.mark.parametrize("stage,status", [("implement", "developing"), ("verify", "validating")])
@pytest.mark.parametrize("legacy", [False, True])
async def test_engine_phase_reaches_current_factory_authority_without_status_translation(
    monkeypatch, stage, status, legacy
):
    service, run, _, effects = await authority(monkeypatch, stage=stage, status=status, legacy=legacy)
    value = await service.authorize(run["id"], stage)
    assert value == {"run": run, "attempt": 2, "fence": "controlled-exact-fence"}
    assert effects == ["ownership", "repository", "permission", "fence"]


async def test_retained_unmarked_running_contract_requires_the_original_owned_task(monkeypatch):
    service, run, task, _ = await authority(monkeypatch, status="running", legacy=True)
    assert (await service.authorize(run["id"], "verify"))["attempt"] == 2
    task["idempotency_key"] = "f" * 32
    with pytest.raises(RuntimeError):
        await service.authorize(run["id"], "verify")


@pytest.mark.parametrize("changed", ["run_phase", "active_task", "task_status", "task_attempt", "task_key", "cancel"])
async def test_wrong_phase_or_replaced_task_cannot_dispatch_or_acquire_an_effect_fence(monkeypatch, changed):
    service, run, task, effects = await authority(monkeypatch)
    if changed == "run_phase":
        run["status"] = "developing"
    elif changed == "active_task":
        run["active_task"] = "implement"
    elif changed == "task_status":
        task["status"] = "paused"
    elif changed == "task_attempt":
        task["attempt"] = 3
    elif changed == "task_key":
        task["idempotency_key"] = "f" * 32
    else:
        run["cancel_request"] = {"status": "requested"}
    with pytest.raises(RuntimeError):
        await service.authorize(run["id"], "verify")
    assert "fence" not in effects


@pytest.mark.parametrize("status", ["running", "paused", "interrupted", "completed"])
async def test_new_work_never_uses_a_generic_or_paused_state_as_stage_authority(monkeypatch, status):
    service, run, _, effects = await authority(monkeypatch, status=status)
    with pytest.raises(RuntimeError):
        await service.authorize(run["id"], "verify")
    assert "fence" not in effects
