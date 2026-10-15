# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicitly provisioned Docker/archive acceptance; no image pull or host fallback."""

import hashlib
import os

import pytest

from firefly_forge.diagnostic_recipes import PythonProfileRequest, build_profile_argv
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.sandbox import SandboxExecutor, SandboxReconciliationRequired
from firefly_forge.sandbox_models import SandboxInput

pytestmark = pytest.mark.skipif(
    not os.environ.get("FORGE_TEST_DOCKER_IMAGE"), reason="Requires an explicitly prepared native Docker profile"
)


async def owned_profile(store, tmp_path, source, *, output=16000):
    from firefly_forge.agent_roles import ProjectRoles
    from firefly_forge.workspace_tools import tool_catalog

    await store.acquire_ownership()
    workspace = tmp_path / "owned-source"
    workspace.mkdir()
    (workspace / "workload.py").write_text(source)
    repository = await store.add_repository(
        RepositoryInput(name="Owned profile", url="https://github.com/test/owned.git")
    )
    config = SandboxInput(
        mode="docker",
        context=os.environ["FORGE_TEST_DOCKER_CONTEXT"],
        image=os.environ["FORGE_TEST_DOCKER_IMAGE"],
        workspace_transport="archive",
        max_output_bytes=output,
        timeout=10,
        command_limit=5,
    )
    run = await store.create_run(
        RunInput(
            repository_id=repository["id"],
            brief="Observe owned fictional workload",
            agent="agentic",
            sandbox=config,
            roles=ProjectRoles(allowed_tools={"implementer": ["execute_command", "profile_python"]}),
        ),
        prepared_tools=tool_catalog({"execute_command": True, "profile_python": True}),
    )
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "implement")
    runner = SandboxExecutor(store, journal)
    engine = Engine(store, tmp_path / "jobs", journal=journal)
    engine.sandbox = runner
    return workspace, run, journal, runner, engine


async def assert_drained(workspace, run, journal, runner):
    rows = await journal.sandbox_executions(run["id"])
    assert rows and all(
        row["status"] in {"removed", "interrupted"} and row["effects"]["cleanup"]["status"] == "done" for row in rows
    )
    assert journal.verify_chain(await journal.entries(run["id"]))
    config = SandboxInput.model_validate(run["sandbox_snapshot"]["configuration"])
    found = await runner._docker(
        config, ["ps", "--all", "--filter", f"label=org.firefly.forge.run={run['id']}", "--format", "{{.Names}}"]
    )
    assert found.exit_code == 0 and not found.output.strip()
    return rows


async def test_actual_native_profile_has_source_image_wal_binding_and_preserves_originals(store, tmp_path):
    source = """import os,pathlib,socket,sys
assert os.getuid()!=0
assert not pathlib.Path('/var/run/docker.sock').exists()
assert sys.argv[1:]==['two words','; literal argument']
try: pathlib.Path('/root/blocked').write_text('blocked')
except OSError: pass
else: raise AssertionError('root filesystem writable')
try: socket.create_connection(('1.1.1.1',443),timeout=.3)
except OSError: pass
else: raise AssertionError('network escaped')
def owned_workload(): return sum(range(2000))
print(owned_workload())
"""
    workspace, run, journal, runner, engine = await owned_profile(store, tmp_path, source)
    original = (workspace / "workload.py").read_bytes()
    result = await engine._profile_python(
        run["id"], run, workspace, PythonProfileRequest(script="workload.py", args=["two words", "; literal argument"])
    )
    assert result["exit_code"] == 0 and "1999000" in result["output"]
    assert "function calls" in result["output"] and "cumtime" in result["output"]
    assert result["report_marker_observed"] and result["observation_only"]
    assert result["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert result["image"] == os.environ["FORGE_TEST_DOCKER_IMAGE"]
    assert (workspace / "workload.py").read_bytes() == original
    assert sorted(path.name for path in workspace.iterdir()) == ["workload.py"]
    rows = await assert_drained(workspace, run, journal, runner)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "removed"
    assert result["receipt"]["execution_id"] == row["execution_id"]
    assert result["receipt"]["request_digest"] == row["request"]["request_digest"]
    assert result["receipt"]["workspace_digest"] == row["request"]["request"]["workspace_digest"]
    assert result["argv"] == row["request"]["request"]["argv"]
    keys = {entry["idempotency_key"] for entry in await journal.entries(run["id"])}
    assert result["receipt"]["start_key"] in keys and result["receipt"]["cleanup_key"] in keys


@pytest.mark.parametrize(
    "source,code,report", [("raise SystemExit(7)\n", 7, True), ("import os\nos._exit(23)\n", 23, False)]
)
async def test_actual_native_profile_preserves_workload_failures(store, tmp_path, source, code, report):
    workspace, run, journal, runner, engine = await owned_profile(store, tmp_path, source)
    result = await engine._profile_python(run["id"], run, workspace, PythonProfileRequest(script="workload.py"))
    assert result["exit_code"] == code and result["report_marker_observed"] is report
    assert (workspace / "workload.py").read_text() == source
    assert all(row["status"] == "removed" for row in await assert_drained(workspace, run, journal, runner))


@pytest.mark.parametrize(
    "source,error", [("import time\ntime.sleep(10)\n", TimeoutError), ("print('x'*50000)\n", RuntimeError)]
)
async def test_actual_native_profile_limits_do_not_allow_unknown_replay(store, tmp_path, source, error):
    workspace, run, journal, runner, engine = await owned_profile(store, tmp_path, source, output=1024)
    request = PythonProfileRequest(script="workload.py", timeout=1)
    argv = build_profile_argv(request, hashlib.sha256(source.encode()).hexdigest())
    with pytest.raises(error):
        await runner.execute(
            run["id"],
            workspace,
            workspace,
            argv,
            1,
            "implement",
            ordinal=1,
            _diagnostic_snapshot=run["sandbox_snapshot"]["digest"],
        )
    before = await journal.sandbox_executions(run["id"])
    with pytest.raises(SandboxReconciliationRequired):
        await runner.execute(
            run["id"],
            workspace,
            workspace,
            argv,
            1,
            "implement",
            ordinal=1,
            _diagnostic_snapshot=run["sandbox_snapshot"]["digest"],
        )
    assert len(await journal.sandbox_executions(run["id"])) == len(before) == 1
    assert (workspace / "workload.py").read_text() == source
    rows = await assert_drained(workspace, run, journal, runner)
    assert rows[0]["effects"]["start"]["evidence"]["outcome"] == "interrupted"


async def test_actual_native_collector_failure_keeps_one_completed_workload(store, tmp_path):
    source = "raise SystemExit(7)\n"
    workspace, run, journal, runner, engine = await owned_profile(store, tmp_path, source)
    request = PythonProfileRequest(script="workload.py")
    argv = build_profile_argv(request, hashlib.sha256(source.encode()).hexdigest())
    observations = []

    def failing(value):
        observations.append(value)
        raise RuntimeError("Owned optional collector failure")

    result = await runner.execute(
        run["id"],
        workspace,
        workspace,
        argv,
        10,
        "implement",
        ordinal=1,
        _diagnostic_snapshot=run["sandbox_snapshot"]["digest"],
        _receipt_sink=failing,
    )
    before = await journal.entries(run["id"])
    retained = await runner.execute(
        run["id"],
        workspace,
        workspace,
        argv,
        10,
        "implement",
        ordinal=1,
        _diagnostic_snapshot=run["sandbox_snapshot"]["digest"],
        _receipt_sink=failing,
    )
    assert result == retained and result.exit_code == 7
    assert len(observations) == 2 and observations[0] == observations[1]
    assert await journal.entries(run["id"]) == before
    assert len(await assert_drained(workspace, run, journal, runner)) == 1
