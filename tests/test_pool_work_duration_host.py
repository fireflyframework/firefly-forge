# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Pool work deadlines are explicit policies; legacy profiles keep their exact wire."""

import pytest
from test_runner_pools import invocation, profile

from firefly_forge.runner_factory_models import FactoryPoolSelection, capture_factory_pool
from firefly_forge.work_duration_models import WorkDurationPolicyV2


def test_old_pool_policy_and_invocation_do_not_gain_new_fields():
    pool = profile()
    value = invocation(pool)
    assert "work_duration" not in pool.model_dump(mode="json")
    assert "work_duration" not in value.model_dump(mode="json")
    assert value.timeout == pool.max_timeout == 300
    factory = FactoryPoolSelection(profile=pool, image=value.image)
    assert "work_duration" not in capture_factory_pool(factory)["configuration"]


@pytest.mark.parametrize("chosen", [None, 864000])
def test_new_pool_policy_can_admit_uncapped_or_explicit_long_work(chosen):
    duration = WorkDurationPolicyV2(timeout_seconds=chosen)
    pool = profile(work_duration=duration)
    value = invocation(pool, work_duration=duration, timeout=chosen)
    pool.validate_invocation(value)
    assert value.timeout == chosen
    factory = FactoryPoolSelection(profile=pool, image=value.image, work_duration=duration)
    assert capture_factory_pool(factory)["configuration"]["work_duration"] == duration.model_dump(mode="json")


def test_old_admin_pool_does_not_silently_grant_unlimited_new_work():
    pool = profile()
    value = invocation(pool, timeout=None, work_duration=WorkDurationPolicyV2())
    with pytest.raises(ValueError, match="duration"):
        pool.validate_invocation(value)


def test_explicit_admin_work_cap_remains_an_immutable_bound():
    pool = profile(work_duration=WorkDurationPolicyV2(timeout_seconds=12))
    for timeout in (None, 13):
        value = invocation(pool, timeout=timeout, work_duration=WorkDurationPolicyV2(timeout_seconds=timeout))
        with pytest.raises(ValueError, match="duration"):
            pool.validate_invocation(value)
    value = invocation(pool, timeout=8, work_duration=WorkDurationPolicyV2(timeout_seconds=8))
    pool.validate_invocation(value)


def test_v2_container_cannot_use_legacy_release_or_unauthenticated_result_reader(tmp_path):
    from dataclasses import replace

    from test_runner_pool_dispatch import authority, dispatcher_module

    duration = WorkDurationPolicyV2()
    old_admin = authority(tmp_path)
    pool = old_admin.profile.model_copy(update={"work_duration": duration})
    admin = replace(old_admin, profile=pool)
    dispatch = dispatcher_module().DockerPoolDispatcher(admin)
    value = invocation(pool, work_duration=duration, timeout=None)
    plan = dispatch.container_plan(value, "e" * 64, "f" * 64)
    assert "OwnerLiveness" in plan[-1] and "owner.begin_drain()" in plan[-1]
    with pytest.raises(ValueError, match="authenticated"):
        dispatch.release_plan(value, "e" * 64)
    with pytest.raises(ValueError, match="authenticated"):
        dispatch.result_plan(value, "e" * 64)
    reader = dispatch.owner_plan(value, "e" * 64)
    assert "--interactive" in reader and "SO_PEERCRED" in reader[-1]
    assert "key" not in reader[:-1]


async def test_uncapped_pool_is_unavailable_and_refused_before_command_intent(tmp_path):
    import asyncio
    from collections import defaultdict
    from types import SimpleNamespace

    from firefly_forge.runner_pool_worker import RunnerPoolWorker

    duration = WorkDurationPolicyV2()
    pool = profile(work_duration=duration)
    value = invocation(pool, work_duration=duration, timeout=None)
    snapshot = capture_factory_pool(FactoryPoolSelection(profile=pool, image=value.image, work_duration=duration))
    worker = object.__new__(RunnerPoolWorker)
    observed = await worker.probe(snapshot)
    assert observed["available"] is False and "run-owned" in observed["reason"]
    worker.locks = defaultdict(asyncio.Lock)
    worker.artifacts = SimpleNamespace(credential_registry=lambda: {})

    async def authorize(*args):
        return {"run": {"sandbox_snapshot": snapshot}, "attempt": 1}

    worker.authority = SimpleNamespace(authorize=authorize)
    with pytest.raises(ValueError, match="run-owned"):
        await worker.execute("a" * 32, tmp_path, tmp_path, ["fixture"], None, "verify")


async def test_controller_refuses_direct_uncapped_admission_and_quarantines_queued_work():
    import asyncio
    from types import SimpleNamespace

    from firefly_forge.runner_pool_execution import RunnerPoolExecutions

    duration = WorkDurationPolicyV2()
    pool = profile(work_duration=duration)
    value = invocation(pool, work_duration=duration, timeout=None)
    controller = object.__new__(RunnerPoolExecutions)
    controller.stopping = False
    controller.admission_lock = asyncio.Lock()
    with pytest.raises(ValueError, match="run-owned"):
        await controller.accept(value, None, b"fictional-source", b"")
    with pytest.raises(ValueError, match="run-owned"):
        await controller.continue_invocation(value, None, "fixture", "a" * 64)
    quarantined = []
    controller.owner = "controlled-owner"
    controller.dispatcher = SimpleNamespace(authority=SimpleNamespace(profile=pool))

    async def observe(*args):
        return {"status": "leased", "owner_id": controller.owner, "epoch": 1, "request": value.model_dump(mode="json")}

    async def quarantine(*args):
        quarantined.append(args)

    controller.pools = SimpleNamespace(observe=observe, quarantine=quarantine)
    claim = {"request": value.model_dump(mode="json"), "id": "fixture", "epoch": 1}
    await controller.execute_claim(claim, None)
    assert len(quarantined) == 1 and "run-owned" in quarantined[0][-1]["reason"]
