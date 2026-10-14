# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Dispatcher contract tests; recorded transports are not native Docker proof."""

import importlib
from uuid import uuid4

import pytest
from runner_test_support import installed_docker_binary

from firefly_forge.runner_pool_models import RunnerInvocation, RunnerPoolProfile

IMAGE = "example/worker@sha256:" + "a" * 64
TRANSFER = "example/transfer@sha256:" + "b" * 64


def dispatcher_module():
    try:
        return importlib.import_module("firefly_forge.runner_pool_dispatch")
    except ModuleNotFoundError:
        pytest.fail("Trusted local Docker dispatcher is not implemented")


def policy():
    return RunnerPoolProfile(
        profile_id="test-pool", daemon_id="owned-daemon", approved_images=(IMAGE,), transfer_image=TRANSFER
    )


def request(pool):
    return RunnerInvocation(
        execution_id=uuid4().hex,
        profile_id=pool.profile_id,
        profile_revision=pool.revision,
        repository_id=uuid4(),
        run_id=uuid4(),
        image=IMAGE,
        argv=("python", "-c", "print(1)"),
        source_digest="c" * 64,
        archive_digest="d" * 64,
        source_bytes=100,
    )


def authority(tmp_path):
    module = dispatcher_module()
    config = tmp_path / "docker"
    config.mkdir(mode=0o700)
    state = tmp_path / "runner"
    state.mkdir(mode=0o700)
    return module.RunnerDockerAuthority(
        profile=policy(),
        docker_binary=installed_docker_binary(),
        context="colima-forge-tests",
        config_directory=config,
        state_directory=state,
        namespace="forge-runner-m2-test",
        uid=10001,
        gid=10001,
    )


def test_container_plan_uses_only_isolated_volume_and_approved_parked_launcher(tmp_path):
    module = dispatcher_module()
    admin = authority(tmp_path)
    dispatch = module.DockerPoolDispatcher(admin)
    command = dispatch.container_plan(request(admin.profile), "e" * 64, "f" * 64)
    assert "--network=none" in command and "--read-only" in command
    assert "--cap-drop=ALL" in command and "--security-opt=no-new-privileges:true" in command
    assert "--user=10001:10001" in command
    assert all("type=bind" not in part and "docker.sock" not in part for part in command)
    assert command[command.index("--entrypoint") + 1] == "/app/.venv/bin/python"
    assert "/usr/bin/timeout" in command[-1]  # fixed script parks before its timeout launcher
    assert command[command.index("--entrypoint") + 2] == IMAGE
    with pytest.raises(ValueError, match="volume"):
        dispatch.container_plan(request(admin.profile), "e" * 64 + ",dst=/private", "f" * 64)


async def test_loss_after_validation_before_daemon_dispatch_closes_create_and_start(tmp_path):
    module = dispatcher_module()
    admin = authority(tmp_path)
    emitted, alive = [], True

    async def guard():
        if not alive:
            raise RuntimeError("Factory ownership lost")

    async def transport(argv, timeout, limit, input_data=None):
        nonlocal alive
        emitted.append(argv)
        if argv[:2] == ["info", "--format"]:
            alive = False  # validation succeeded, then ownership disappeared during identity query
            return 0, b"owned-daemon\n"
        raise AssertionError("A fenced Docker mutation was emitted")

    dispatch = module.DockerPoolDispatcher(admin, transport=transport)
    for argv in (["create", "--name", "test"], ["start", "test"]):
        alive = True
        with pytest.raises(RuntimeError, match="ownership"):
            await dispatch.fenced_control(argv, guard)
    assert all(argv[0] == "info" for argv in emitted)


async def test_wrong_daemon_identity_and_ambiguous_dispatch_are_not_retried(tmp_path):
    module = dispatcher_module()
    admin = authority(tmp_path)
    calls = []

    async def guard():
        return None

    async def wrong(argv, timeout, limit, input_data=None):
        calls.append(argv)
        return 0, b"another-daemon"

    dispatch = module.DockerPoolDispatcher(admin, transport=wrong)
    with pytest.raises(RuntimeError, match="identity"):
        await dispatch.fenced_control(["create", "test"], guard)
    assert len(calls) == 1

    async def uncertain(argv, timeout, limit, input_data=None):
        calls.append(argv)
        if argv[0] == "info":
            return 0, b"owned-daemon"
        raise TimeoutError("Daemon response lost")

    calls.clear()
    dispatch = module.DockerPoolDispatcher(admin, transport=uncertain)
    with pytest.raises(TimeoutError):
        await dispatch.fenced_control(["create", "test"], guard)
    assert [argv[0] for argv in calls] == ["info", "create"]


async def test_real_database_lock_loss_inside_identity_check_prevents_mutation(tmp_path, store):
    from sqlalchemy import text

    from firefly_forge.runner_pool_models import FactoryFence
    from firefly_forge.runner_pools import RunnerPools

    module = dispatcher_module()
    admin = authority(tmp_path)
    ledger = RunnerPools(store.session_provider, store.schema)
    await ledger.initialize()
    await store.acquire_ownership()
    async with store._unit() as session:
        started = await session.scalar(
            text("SELECT backend_start FROM pg_stat_activity WHERE pid=:pid"), {"pid": store._ownership_pid}
        )
    fence = FactoryFence(factory_schema=store.schema, pid=store._ownership_pid, backend_started_at=started)
    invocation = request(admin.profile)
    await ledger.submit(admin.profile, invocation)
    claim = await ledger.claim(admin.profile, "controller-a", fence)
    await ledger.effect_intent(admin.profile, claim["id"], "controller-a", claim["epoch"], fence, "create")
    mutations = []

    async def guard():
        await ledger.authorize(admin.profile, claim["id"], "controller-a", claim["epoch"], fence)

    async def transport(argv, timeout, limit, input_data=None):
        if argv[0] == "info":
            await store.release_ownership()
            return 0, b"owned-daemon"
        mutations.append(argv)
        return 0, b"unexpected"

    dispatch = module.DockerPoolDispatcher(admin, transport=transport)
    with pytest.raises(RuntimeError, match="ownership"):
        await dispatch.fenced_control(["create", "--name", dispatch.name(invocation)], guard)
    assert mutations == []
    retained = await ledger.observe(admin.profile, invocation.execution_id)
    assert retained["status"] == "creating"  # intent retained; no speculative receipt or release
    assert (await ledger.history(admin.profile, invocation.execution_id))[-1]["kind"] == "create_intent"


def test_transfer_helper_is_trusted_bounded_and_gate_release_is_nonblocking_one_shot(tmp_path):
    module = dispatcher_module()
    admin = authority(tmp_path)
    dispatch = module.DockerPoolDispatcher(admin)
    invocation = request(admin.profile)
    transfer = dispatch.transfer_plan(invocation)
    assert transfer[transfer.index("--entrypoint") + 2] == TRANSFER
    assert "--user=0:0" in transfer and "--cap-drop=ALL" in transfer and "--cap-add=CHOWN" in transfer
    assert "--network=none" in transfer and "--read-only" in transfer
    assert all("type=bind" not in part and "docker.sock" not in part for part in transfer)
    assert "('/workspace','/forge-git')" in transfer[-1]
    assert "signal.alarm(30)" in transfer[-1]
    gate = dispatch.release_plan(invocation, "e" * 64)
    assert gate[:3] == ["exec", "e" * 64, "/app/.venv/bin/python"]
    assert "O_NONBLOCK" in gate[-1] and "O_NOFOLLOW" in gate[-1]
    assert invocation.argv[0] not in gate[-1]  # gate does not accept a second project command
    with pytest.raises(ValueError, match="identity"):
        dispatch.release_plan(invocation, "--privileged")


def test_project_writes_use_bounded_tmpfs_and_input_is_readonly(tmp_path):
    module = dispatcher_module()
    admin = authority(tmp_path)
    dispatch = module.DockerPoolDispatcher(admin)
    plan = dispatch.container_plan(request(admin.profile), "e" * 64, "f" * 64)
    assert any("dst=/forge-input,readonly" in part for part in plan)
    assert any(part.startswith("--tmpfs=/workspace:") and "size=67108864" in part for part in plan)
    assert "--init" not in plan  # supervisor must be namespace PID1
    assert "PR_SET_DUMPABLE" in plan[-1]
    assert "SO_PEERCRED" in dispatch.result_plan(request(admin.profile), "e" * 64)[-1]
    assert "signal.alarm(60)" in plan[-1]


async def test_fixed_exporter_reuses_hardened_reader_and_rejects_symlinks(tmp_path):
    import io
    import sys
    import tarfile

    from firefly_forge.runner_backends import _bounded_process

    module = dispatcher_module()
    admin = authority(tmp_path)
    dispatcher = module.DockerPoolDispatcher(admin)
    invocation = request(admin.profile)
    workspace = tmp_path / "result"
    workspace.mkdir()
    (workspace / "main.py").write_text("retained result")
    script = dispatcher.collect_plan(invocation, "e" * 64)[-1]
    assert "os.O_NOFOLLOW" in script and "st_nlink != 1" in script
    local_script = script.replace("Path('/workspace')", "Path(" + repr(str(workspace)) + ")")
    code, raw = await _bounded_process([sys.executable, "-I", "-S", "-c", local_script], tmp_path, 5, 2_000_000)
    assert code == 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as stream:
        assert stream.extractfile("main.py").read() == b"retained result"
    outside = tmp_path / "outside"
    outside.write_text("must stay private")
    (workspace / "escape").symlink_to(outside)
    code, raw = await _bounded_process([sys.executable, "-I", "-S", "-c", local_script], tmp_path, 5, 2_000_000)
    assert code != 0 and b"only regular files" in raw


async def test_runtime_availability_requires_all_cached_pinned_images_without_pull(tmp_path):
    module = dispatcher_module()
    admin = authority(tmp_path)
    calls = []

    async def transport(argv, timeout, limit, input_data=None):
        calls.append(argv)
        if argv[0] == "info":
            return 0, b"owned-daemon"
        return (1, b"No such image") if argv[2] == TRANSFER else (0, ("sha256:" + "f" * 64).encode())

    dispatch = module.DockerPoolDispatcher(admin, transport=transport)
    with pytest.raises(RuntimeError, match="cached"):
        await dispatch.verify_images()
    assert all(argv[0] in {"info", "image"} for argv in calls)
