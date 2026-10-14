# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Durable execution failure boundaries with actual PostgreSQL and recorded Docker transport."""

import importlib
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from firefly_forge.runner_pool_artifacts import RunnerPoolArtifacts, source_tree_digest
from firefly_forge.runner_pool_dispatch import DockerPoolDispatcher, RunnerDockerAuthority
from firefly_forge.runner_pool_models import FactoryFence, RunnerInvocation, RunnerPoolProfile
from firefly_forge.runner_pools import RunnerPools
from firefly_forge.sandbox import pack_workspace

IMAGE = "example/worker@sha256:" + "a" * 64


def execution_module():
    try:
        return importlib.import_module("firefly_forge.runner_pool_execution")
    except ModuleNotFoundError:
        pytest.fail("Durable runner execution lifecycle is not implemented")


@pytest.mark.parametrize("tampered", [False, True])
async def test_unknown_create_or_tampered_input_is_quarantined_without_replay(store, tmp_path, tampered):
    module = execution_module()
    await store.acquire_ownership()
    async with store._unit() as session:
        started = await session.scalar(
            text("SELECT backend_start FROM pg_stat_activity WHERE pid=:pid"), {"pid": store._ownership_pid}
        )
    fence = FactoryFence(factory_schema=store.schema, pid=store._ownership_pid, backend_started_at=started)
    profile = RunnerPoolProfile(
        profile_id="owned-test", daemon_id="owned-engine", approved_images=(IMAGE,), transfer_image=IMAGE
    )
    pools = RunnerPools(store.session_provider, store.schema)
    await pools.initialize()
    config = tmp_path / "docker"
    config.mkdir(mode=0o700)
    state = tmp_path / "runner-artifacts"
    state.mkdir(mode=0o700)
    authority = RunnerDockerAuthority(
        profile,
        Path("/usr/local/bin/docker"),
        "colima-forge-tests",
        config,
        state,
        "forge-runner-m2-execution-test",
        10001,
        10001,
    )
    effects = []

    async def transport(argv, timeout, limit, input_data=None):
        if argv[0] == "info":
            return 0, b"owned-engine"
        effects.append(argv)
        raise TimeoutError("Controlled ambiguous create response")

    dispatcher = DockerPoolDispatcher(authority, transport=transport)
    artifacts = RunnerPoolArtifacts(state)
    executor = module.RunnerPoolExecutions(pools, artifacts, dispatcher)
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print(1)")
    archive, digest = pack_workspace(source, profile.max_source_bytes)
    request = RunnerInvocation(
        execution_id=uuid4().hex,
        profile_id=profile.profile_id,
        profile_revision=profile.revision,
        repository_id=uuid4(),
        run_id=uuid4(),
        image=IMAGE,
        argv=("python", "main.py"),
        source_digest=source_tree_digest(source, profile.max_source_bytes),
        archive_digest=digest,
        source_bytes=len(archive),
    )
    artifacts.prepare(profile, request, archive)
    artifacts.retain(profile, request, "stdin", b"")
    await pools.submit(profile, request, fence=fence)
    claim = await pools.claim(profile, executor.owner, fence)
    if tampered:
        (state / request.execution_id / "stdin.bin").write_bytes(b"changed")
    await executor.execute_claim(claim, fence)
    assert (await pools.observe(profile, request.execution_id))["status"] == "quarantined"
    assert len(effects) == (0 if tampered else 1)
    if effects:
        assert effects[0][0] == "create"
    await executor.execute_claim(claim, fence)
    assert len(effects) == (0 if tampered else 1)
    assert await pools.claim(profile, "other-controller", fence) is None


@pytest.mark.parametrize("observed_helper", [False, True])
async def test_reconciliation_never_releases_unknown_create_without_resource_observation(
    store, tmp_path, observed_helper
):
    module = execution_module()
    await store.acquire_ownership()
    async with store._unit() as session:
        started = await session.scalar(
            text("SELECT backend_start FROM pg_stat_activity WHERE pid=:pid"), {"pid": store._ownership_pid}
        )
    fence = FactoryFence(factory_schema=store.schema, pid=store._ownership_pid, backend_started_at=started)
    profile = RunnerPoolProfile(
        profile_id="owned-test", daemon_id="owned-engine", approved_images=(IMAGE,), transfer_image=IMAGE
    )
    pools = RunnerPools(store.session_provider, store.schema)
    await pools.initialize()
    config = tmp_path / "docker"
    state = tmp_path / "runner-artifacts"
    config.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    authority = RunnerDockerAuthority(
        profile,
        Path("/usr/local/bin/docker"),
        "colima-forge-tests",
        config,
        state,
        "forge-runner-m2-reconcile-test",
        10001,
        10001,
    )
    resources = {}
    removals = []

    async def transport(argv, timeout, limit, input_data=None):
        import json

        if argv[0] == "info":
            return 0, b"owned-engine"
        if argv[:2] == ["container", "inspect"]:
            value = resources.get(argv[2])
            return (0, json.dumps([value]).encode()) if value else (1, b"No such container")
        if argv[:2] == ["container", "rm"]:
            removals.append(argv[-1])
            resources.clear()
            return 0, b""
        raise AssertionError("Reconciliation must not create or start")

    dispatcher = DockerPoolDispatcher(authority, transport=transport)
    executor = module.RunnerPoolExecutions(pools, RunnerPoolArtifacts(state), dispatcher)
    request = RunnerInvocation(
        execution_id=uuid4().hex,
        profile_id=profile.profile_id,
        profile_revision=profile.revision,
        repository_id=uuid4(),
        run_id=uuid4(),
        image=IMAGE,
        argv=("true",),
        source_digest="b" * 64,
        archive_digest="c" * 64,
        source_bytes=10240,
    )
    await pools.submit(profile, request, fence=fence)
    claim = await pools.claim(profile, executor.owner, fence)
    name = dispatcher.name(request)
    target = {
        "daemon_id": profile.daemon_id,
        "namespace": authority.namespace,
        "container_name": name,
        "transfer_name": name + "-transfer",
        "labels": dispatcher.labels(request),
    }
    await pools.effect_intent(profile, claim["id"], executor.owner, claim["epoch"], fence, "create", payload=target)
    await pools.quarantine(profile, claim["id"], executor.owner, claim["epoch"], {"reason": "create_response_lost"})
    if observed_helper:
        value = {
            "Id": "a" * 64,
            "Name": "/" + name + "-transfer",
            "Config": {"Labels": dispatcher.labels(request)},
            "State": {"Running": False},
            "Mounts": [],
        }
        resources[name + "-transfer"] = value
        resources[value["Id"]] = value
        result = await executor.reconcile(request.execution_id, fence)
        assert result["status"] == "finished" and result["receipt"]["outcome"] == "unknown"
        assert removals == [value["Id"]]
    else:
        with pytest.raises(RuntimeError, match="unknown create"):
            await executor.reconcile(request.execution_id, fence)
        assert (await pools.observe(profile, request.execution_id))["status"] == "quarantined"
        assert not removals


async def test_controller_stop_cannot_admit_new_work_from_task_finalizers(store, tmp_path):
    import asyncio

    module = execution_module()
    admissions = []

    class Pools:
        async def claim(self, *args):
            admissions.append(args)
            return None

    class Dispatcher:
        authority = type(
            "Authority",
            (),
            {
                "profile": RunnerPoolProfile(
                    profile_id="owned-test", daemon_id="owned-engine", approved_images=(IMAGE,), transfer_image=IMAGE
                )
            },
        )()

    executor = module.RunnerPoolExecutions(Pools(), None, Dispatcher())
    executor.fence = object()
    entered = asyncio.Event()

    async def pending():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await executor.pump()

    task = asyncio.create_task(pending())
    executor.tasks["a" * 32] = task
    await entered.wait()
    await executor.stop()
    assert not admissions and not executor.tasks
