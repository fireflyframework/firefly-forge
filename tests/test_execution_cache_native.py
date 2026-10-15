# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual local containers must verify source despite transported stale bytecode."""

import asyncio
import hashlib
import json
import os
import py_compile
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from runner_test_support import installed_docker_binary
from sqlalchemy import text

from firefly_forge.journal import Journal
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.runner_pool_artifacts import RunnerPoolArtifacts, source_tree_digest
from firefly_forge.runner_pool_dispatch import DockerPoolDispatcher, RunnerDockerAuthority
from firefly_forge.runner_pool_execution import RunnerPoolExecutions
from firefly_forge.runner_pool_models import FactoryFence, RunnerInvocation, RunnerPoolProfile
from firefly_forge.runner_pools import RunnerPools
from firefly_forge.sandbox import SandboxExecutor, pack_workspace, unpack_workspace
from firefly_forge.sandbox_models import SandboxInput

pytestmark = pytest.mark.skipif(
    not os.environ.get("FORGE_TEST_DOCKER_IMAGE"), reason="Native Docker image not configured"
)


def cached_product(workspace, current, cached):
    path = workspace / "product.py"
    path.write_text(f"stock = {cached}\n")
    os.utime(path, (0, 0))
    cache = Path(py_compile.compile(str(path), doraise=True))
    content = cache.read_bytes()
    path.write_text(f"stock = {current}\n")
    os.utime(path, (0, 0))
    return cache, content


@pytest.mark.parametrize("current,cached,expected", [(4, 5, 1), (5, 4, 0)])
async def test_actual_legacy_archive_reads_current_source_and_preserves_cache(
    store, tmp_path, current, cached, expected
):
    await store.acquire_ownership()
    workspace = tmp_path / "source"
    workspace.mkdir()
    cache, content = cached_product(workspace, current, cached)
    repository = await store.add_repository(
        RepositoryInput(name="Owned cached stock", url="https://example.invalid/stock")
    )
    config = SandboxInput(
        mode="docker",
        image=os.environ["FORGE_TEST_DOCKER_IMAGE"],
        context=os.environ.get("FORGE_TEST_DOCKER_CONTEXT", "colima-forge-tests"),
        workspace_transport="archive",
    )
    run = await store.create_run(RunInput(repository_id=repository["id"], brief="Check current source", sandbox=config))
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "verify")
    runner = SandboxExecutor(store, journal)
    result = await runner.execute(
        run["id"],
        workspace,
        workspace,
        ["/app/.venv/bin/python", "-c", "from product import stock; assert stock == 5"],
        20,
        "verify",
    )
    assert result.exit_code == expected, result.output
    assert cache.read_bytes() == content
    assert (workspace / "product.py").read_text() == f"stock = {current}\n"
    assert all(row["status"] == "removed" for row in await journal.sandbox_executions(run["id"]))
    assert Journal.verify_chain(await journal.entries(run["id"]))


@pytest.mark.parametrize("current,cached,expected", [(4, 5, 1), (5, 4, 0)])
async def test_actual_pool_launcher_reads_current_source_and_retains_bounded_receipt(
    store, tmp_path, current, cached, expected
):
    docker = installed_docker_binary()
    context = os.environ.get("FORGE_TEST_DOCKER_CONTEXT", "colima-forge-tests")
    detail = json.loads(subprocess.check_output([str(docker), "context", "inspect", context], text=True))[0]
    assert detail["Endpoints"]["docker"]["Host"].startswith("unix://")
    configuration = tmp_path / "docker"
    configuration.mkdir(mode=0o700)
    exported = subprocess.check_output([str(docker), "context", "export", context, "-"])
    subprocess.run(
        [str(docker), "--config", str(configuration), "context", "import", context, "-"],
        input=exported,
        check=True,
        capture_output=True,
    )
    assert not (configuration / "config.json").exists()
    daemon = subprocess.check_output(
        [str(docker), "--config", str(configuration), "--context", context, "info", "--format", "{{.ID}}"], text=True
    ).strip()
    image = os.environ["FORGE_TEST_DOCKER_IMAGE"]
    profile = RunnerPoolProfile(
        profile_id="owned-cache",
        daemon_id=daemon,
        approved_images=(image,),
        transfer_image=image,
        max_source_bytes=4_194_304,
        max_memory_mb=256,
        memory_budget_mb=512,
        max_timeout=20,
    )
    state = tmp_path / "artifacts"
    state.mkdir(mode=0o700)
    namespace = "forge-runner-cache-" + uuid4().hex[:12]
    dispatcher = DockerPoolDispatcher(
        RunnerDockerAuthority(profile, docker, context, configuration, state, namespace, 10001, 10001)
    )
    await store.acquire_ownership()
    async with store._unit() as session:
        started = await session.scalar(
            text("SELECT backend_start FROM pg_stat_activity WHERE pid=:pid"), {"pid": store._ownership_pid}
        )
    fence = FactoryFence(factory_schema=store.schema, pid=store._ownership_pid, backend_started_at=started)
    runner_schema = store.schema + "_cache_runner"
    pools = RunnerPools(store.session_provider, runner_schema, factory_schema=store.schema)
    await pools.initialize()
    artifacts = RunnerPoolArtifacts(state)
    executor = RunnerPoolExecutions(pools, artifacts, dispatcher)
    workspace = tmp_path / "source"
    workspace.mkdir()
    cache, content = cached_product(workspace, current, cached)
    raw, digest = pack_workspace(workspace, profile.max_source_bytes)
    invocation = RunnerInvocation(
        execution_id=uuid4().hex,
        profile_id=profile.profile_id,
        profile_revision=profile.revision,
        repository_id=uuid4(),
        run_id=uuid4(),
        image=image,
        argv=("/app/.venv/bin/python", "-c", "from product import stock; assert stock == 5"),
        timeout=20,
        memory_mb=256,
        source_digest=source_tree_digest(workspace, profile.max_source_bytes),
        archive_digest=digest,
        source_bytes=len(raw),
    )
    try:
        await executor.accept(invocation, fence, raw, b"")
        async with asyncio.timeout(60):
            while True:
                observed = await pools.observe(profile, invocation.execution_id)
                if observed["status"] in {"finished", "quarantined"}:
                    break
                await asyncio.sleep(0.05)
        assert observed["status"] == "finished", observed
        receipt = observed["receipt"]
        assert receipt["exit_code"] == expected and receipt["drained"] is True
        returned = artifacts.read(profile, invocation, "result", receipt["result"])
        export = tmp_path / "export"
        unpack_workspace(returned, export, profile.max_source_bytes)
        assert (export / "product.py").read_text() == f"stock = {current}\n"
        assert (export / "__pycache__" / cache.name).read_bytes() == content
        assert cache.read_bytes() == content
        history = await pools.history(profile, invocation.execution_id)
        assert sum(event["kind"] == "start_intent" for event in history) == 1
        assert (
            receipt["output"]["sha256"]
            == hashlib.sha256(artifacts.read(profile, invocation, "output", receipt["output"])).hexdigest()
        )
    finally:
        await executor.stop()
        for name in (dispatcher.name(invocation), dispatcher.name(invocation) + "-transfer"):
            resource = await dispatcher.inspect(invocation, name)
            if resource is not None:
                code, _ = await dispatcher.transport(
                    ["container", "rm", "--force", "--volumes", resource["Id"]], 15, 8192
                )
                assert code == 0
        async with store._unit() as session:
            await session.execute(text(f'DROP SCHEMA "{runner_schema}" CASCADE'))
