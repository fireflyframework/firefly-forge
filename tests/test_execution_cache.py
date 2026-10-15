# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Project verification must observe current source rather than prior Python caches."""

import asyncio
import json
import os
import py_compile
import subprocess
import sys
from pathlib import Path

import pytest
from test_repair_proposal_guard import failed_run

from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.process import execute
from firefly_forge.providers import AgentResult
from firefly_forge.runner_backends import HostRunnerBackend, _bounded_process
from firefly_forge.sandbox import SandboxExecutor
from firefly_forge.sandbox_models import SandboxInput


def stock_source(path, value):
    path.write_text(f"stock = {value}\n")
    os.utime(path, (1700000000, 1700000000))


async def test_bad_source_cannot_pass_native_verification_behind_good_cached_bytecode(store, tmp_path):
    run = await failed_run(store, tmp_path)
    retained = {}

    class CachedWorker:
        async def execute(self, workspace, *args):
            path = workspace / "product.py"
            stock_source(path, 5)
            cache = Path(py_compile.compile(str(path), doraise=True))
            retained.update(path=cache, bytes=cache.read_bytes())
            stock_source(path, 4)
            return AgentResult("Owned unchanged bad stock source", "fixture")

    journal = Journal(store)
    engine = Engine(store, tmp_path / "jobs", lambda _: CachedWorker(), journal=journal)
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "failed", result.get("error")
    receipts = [e for e in await journal.entries(run["id"]) if e["kind"] == "receipt" and e["task_id"] == "verify"]
    assert [e["payload"]["checks"][0]["exit_code"] for e in receipts] == [1]
    assert not result["artifacts"]
    assert retained["path"].read_bytes() == retained["bytes"]


async def test_repaired_source_passes_native_verification_despite_prior_failed_cache(store, tmp_path):
    run = await failed_run(store, tmp_path, limit=2)
    retained = {}

    class RepairingWorker:
        calls = 0

        async def execute(self, workspace, *args):
            self.calls += 1
            path = workspace / "product.py"
            stock_source(path, 4 if self.calls == 1 else 5)
            if self.calls == 1:
                cache = Path(py_compile.compile(str(path), doraise=True))
                retained.update(path=cache, bytes=cache.read_bytes())
            return AgentResult("Owned stock repair", "fixture")

    worker = RepairingWorker()
    journal = Journal(store)
    engine = Engine(store, tmp_path / "jobs", lambda _: worker, journal=journal)
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "succeeded", result.get("error")
    assert worker.calls == 2
    entries = await journal.entries(run["id"])
    assert [
        e["payload"]["checks"][0]["exit_code"] for e in entries if e["kind"] == "receipt" and e["task_id"] == "verify"
    ] == [1, 0]
    assert Journal.verify_chain(entries)
    assert retained["path"].read_bytes() == retained["bytes"]


async def host_command(kind, argv, cwd, timeout=5):
    if kind == "direct":
        return await execute(argv, cwd, timeout)
    return await HostRunnerBackend().run(SandboxInput(), argv, cwd, timeout, 1024)


@pytest.mark.parametrize("kind", ["direct", "host"])
async def test_commands_ignore_inherited_cache_namespace_and_preserve_existing_cache(kind, tmp_path, monkeypatch):
    workspace = tmp_path / "source"
    workspace.mkdir()
    path = workspace / "product.py"
    stock_source(path, 4)
    caller = tmp_path / "caller-cache"
    subprocess.run(
        [sys.executable, "-c", "import product"],
        cwd=workspace,
        env={"PYTHONPYCACHEPREFIX": str(caller)},
        check=True,
    )
    caches = {p: p.read_bytes() for p in caller.rglob("*.pyc")}
    stock_source(path, 5)
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(caller))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "0")
    script = (
        "import json,sys; from pathlib import Path; from product import stock; "
        "print(json.dumps({'stock':stock,'prefix':sys.pycache_prefix,'dontwrite':sys.dont_write_bytecode,"
        "'owner_mode':Path(sys.pycache_prefix).parent.stat().st_mode & 0o777}))"
    )
    prefixes = []
    for _ in range(2):
        result = await host_command(kind, [sys.executable, "-c", script], workspace)
        assert result.exit_code == 0, result.output
        observed = json.loads(result.output)
        assert observed["stock"] == 5
        assert observed["dontwrite"] is True and observed["owner_mode"] == 0o700
        prefix = Path(observed["prefix"])
        assert not prefix.is_relative_to(workspace) and not prefix.is_relative_to(caller)
        assert not prefix.parent.exists()
        prefixes.append(prefix)
    assert prefixes[0] != prefixes[1]
    assert all(p.read_bytes() == content for p, content in caches.items())
    assert not (workspace / "__pycache__").exists()


@pytest.mark.parametrize("kind", ["direct", "host"])
@pytest.mark.parametrize("failure", ["spawn", "timeout"])
async def test_owned_cache_namespace_is_cleaned_after_command_failure(kind, failure, tmp_path, monkeypatch):
    original = asyncio.create_subprocess_exec
    observed = []

    async def observe(*args, **kwargs):
        value = kwargs["env"].get("PYTHONPYCACHEPREFIX")
        observed.append(Path(value).parent if value else None)
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe)
    if failure == "spawn":
        argv = [str(tmp_path / "missing-owned-executable")]
        expected = FileNotFoundError
    else:
        argv = [sys.executable, "-c", "import time; time.sleep(30)"]
        expected = TimeoutError
    with pytest.raises(expected):
        await host_command(kind, argv, tmp_path, timeout=0.1)
    assert observed and all(path is not None for path in observed)
    assert all(not path.exists() for path in observed)


@pytest.mark.parametrize("kind", ["direct", "host"])
async def test_explicit_child_environment_cannot_select_or_reenable_python_cache(kind, tmp_path):
    caller = tmp_path / "caller-cache"
    environment = {"PYTHONPYCACHEPREFIX": str(caller), "PYTHONDONTWRITEBYTECODE": "0"}
    script = "import json,sys; print(json.dumps({'prefix':sys.pycache_prefix,'dontwrite':sys.dont_write_bytecode}))"
    argv = [sys.executable, "-c", script]
    if kind == "direct":
        result = await execute(argv, tmp_path, env=environment)
        code, output = result.exit_code, result.output
    else:
        code, data = await _bounded_process(argv, tmp_path, 5, 1024, environment=environment)
        output = data.decode()
    assert code == 0, output
    observed = json.loads(output)
    assert observed["dontwrite"] is True
    prefix = Path(observed["prefix"])
    assert not prefix.is_relative_to(caller) and not prefix.parent.exists()
    assert not caller.exists()


@pytest.mark.parametrize("kind", ["direct", "host"])
async def test_cancelled_project_command_terminates_before_owned_cache_cleanup(kind, tmp_path, monkeypatch):
    original = asyncio.create_subprocess_exec
    started = asyncio.Event()
    retained = {}

    async def observe(*args, **kwargs):
        value = kwargs["env"].get("PYTHONPYCACHEPREFIX")
        retained["cache"] = Path(value).parent if value else None
        retained["process"] = await original(*args, **kwargs)
        started.set()
        return retained["process"]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe)
    task = asyncio.create_task(host_command(kind, [sys.executable, "-c", "import time; time.sleep(30)"], tmp_path))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert retained["process"].returncode is not None
    assert retained["cache"] is not None and not retained["cache"].exists()


async def test_legacy_container_uses_its_fresh_tmpfs_for_python_cache_policy(store, tmp_path):
    sandbox = SandboxExecutor(store, Journal(store))
    config = SandboxInput(mode="docker", image="example/worker@sha256:" + "1" * 64, context="colima-forge-tests")
    command = sandbox._create_argv(config, tmp_path, tmp_path, ["python", "-c", "print(1)"], "owned", {}, git_root=None)
    environment = dict(command[i + 1].split("=", 1) for i, value in enumerate(command) if value == "--env")
    assert environment.get("PYTHONDONTWRITEBYTECODE") == "1"
    assert environment.get("PYTHONPYCACHEPREFIX") == "/tmp/forge-python-cache"
    assert command.count("--tmpfs") == 1
    assert command[command.index("--tmpfs") + 1] == "/tmp:rw,nosuid,nodev,size=134217728,mode=1777"
    assert command[-2:] == ["-c", "print(1)"]


def test_pool_container_keeps_original_supervisor_and_fresh_python_cache_policy(tmp_path):
    from test_runner_pool_dispatch import authority, request

    from firefly_forge.runner_pool_dispatch import DockerPoolDispatcher

    admin = authority(tmp_path)
    invocation = request(admin.profile)
    command = DockerPoolDispatcher(admin).container_plan(invocation, "e" * 64, "f" * 64)
    environment = dict(value.removeprefix("--env=").split("=", 1) for value in command if value.startswith("--env="))
    assert environment.get("PYTHONDONTWRITEBYTECODE") == "1"
    assert environment.get("PYTHONPYCACHEPREFIX") == "/tmp/forge-python-cache"
    assert "--tmpfs=/tmp:rw,nosuid,nodev,size=134217728,mode=1777" in command
    assert command[command.index("--entrypoint") + 1] == "/app/.venv/bin/python"
    assert command[-4:-1] == ["-I", "-S", "-c"]
