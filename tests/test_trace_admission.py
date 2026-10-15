# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native tracing admission and exact retained receipt binding."""

import pytest
from test_profile_admission import IMAGE, snapshot

from firefly_forge import diagnostic_recipes as recipes
from firefly_forge.sandbox_models import capture_sandbox


@pytest.mark.asyncio
async def test_engine_trace_observation_binds_exact_command_and_preserves_workload_exit(store, tmp_path):
    import hashlib

    from firefly_forge.agent_roles import ProjectRoles
    from firefly_forge.engine import Engine
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.process import ProcessResult
    from firefly_forge.sandbox_models import SandboxInput
    from firefly_forge.workspace_tools import tool_catalog

    repo = await store.add_repository(RepositoryInput(name="owned", url="https://github.com/test/owned.git"))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            brief="Observe owned workload",
            sandbox=SandboxInput.model_validate(snapshot()["configuration"]),
            roles=ProjectRoles(allowed_tools={"implementer": ["execute_command", "trace_python"]}),
        ),
        prepared_tools=tool_catalog({"execute_command": True, "trace_python": True}),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = b"raise SystemExit(7)\r\n"
    (workspace / "workload.py").write_bytes(source)
    calls = []

    class Runner:
        async def execute(self, *args, **kwargs):
            calls.append((args, kwargs))
            kwargs["_receipt_sink"](
                {
                    "execution_id": "a" * 32,
                    "request_digest": "b" * 64,
                    "snapshot_digest": run["sandbox_snapshot"]["digest"],
                    "workspace_digest": "c" * 64,
                    "start_key": "a" * 32 + ":start",
                    "cleanup_key": "a" * 32 + ":cleanup",
                }
            )
            return ProcessResult(args[3], 7, "retained workload failure\nFORGE_PYTHON_TRACE_V1\n" + "x" * 17000)

    engine = Engine(store, tmp_path / "jobs")
    engine.sandbox = Runner()
    result = await engine._trace_python(run["id"], run, workspace, recipes.PythonTraceRequest(script="workload.py"))
    assert result["recipe"] == "forge-python-line-trace-v1"
    assert result["source_sha256"] == hashlib.sha256(source).hexdigest()
    assert result["image"] == IMAGE and result["exit_code"] == 7
    assert result["observation_only"] is True and result["output_truncated"] is True
    assert len(result["output"]) == 16000
    assert result["receipt"]["start_key"] == "a" * 32 + ":start"
    assert result["receipt"]["cleanup_key"] == "a" * 32 + ":cleanup"
    assert result["argv"] == calls[0][0][3]
    assert calls[0][1]["_diagnostic_snapshot"] == run["sandbox_snapshot"]["digest"]
    assert (workspace / "workload.py").read_bytes() == source


@pytest.mark.asyncio
async def test_trace_without_executor_or_grants_never_uses_host(store, tmp_path):
    from firefly_forge.engine import Engine
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.sandbox_models import SandboxInput

    repo = await store.add_repository(RepositoryInput(name="owned", url="https://github.com/test/owned.git"))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"], brief="Observe", sandbox=SandboxInput.model_validate(snapshot()["configuration"])
        )
    )
    (tmp_path / "workload.py").write_text("raise AssertionError('must not execute')")
    engine = Engine(store, tmp_path / "jobs")
    with pytest.raises(ValueError, match="profiling|tracing|Tracing"):
        await engine._trace_python(run["id"], run, tmp_path, recipes.PythonTraceRequest(script="workload.py"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy,available,expected",
    [
        (snapshot(), True, "available"),
        (snapshot(network="egress"), True, "not_configured"),
        (snapshot(workspace_transport="bind"), True, "not_configured"),
        (capture_sandbox(None), True, "not_configured"),
        (snapshot(), False, "not_configured"),
    ],
)
async def test_worker_readiness_admits_only_the_trace_policy(policy, available, expected):
    from types import SimpleNamespace

    from firefly_forge.runner_configuration import RunnerConfiguration

    async def probe(value):
        return {"available": available}

    runner = object.__new__(RunnerConfiguration)
    runner.store = SimpleNamespace(runtime_roles=None)
    runner.executor = SimpleNamespace(probe=probe)
    result = {tool["id"]: tool for tool in await runner.capabilities({"sandbox_snapshot": policy})}
    assert result["trace_python"]["availability"] == expected


@pytest.mark.asyncio
async def test_api_readiness_remains_worker_unverified_for_a_trace_policy():
    from types import SimpleNamespace

    from firefly_forge.runner_configuration import RunnerConfiguration

    runner = object.__new__(RunnerConfiguration)
    runner.store = SimpleNamespace(runtime_roles=SimpleNamespace(role="api"))
    result = {tool["id"]: tool for tool in await runner.capabilities({"sandbox_snapshot": snapshot()})}
    assert result["trace_python"]["availability"] == "worker_unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "grants,enabled",
    [(["execute_command"], False), (["trace_python"], False), (["execute_command", "trace_python"], True)],
)
async def test_engine_supplies_the_optional_trace_hook_to_the_native_worker(store, tmp_path, grants, enabled):
    from test_role_execution import fixture_run

    from firefly_forge.agent_roles import ProjectRoles
    from firefly_forge.engine import Engine
    from firefly_forge.journal import Journal
    from firefly_forge.models import RunInput
    from firefly_forge.process import ProcessResult
    from firefly_forge.providers import ConfiguredProvider, ProviderLimitError
    from firefly_forge.sandbox_models import SandboxInput
    from firefly_forge.workspace_tools import tool_catalog

    initial = await fixture_run(store, tmp_path)
    run = await store.create_run(
        RunInput(
            repository_id=initial["repository_id"],
            brief="Inspect optional diagnostic binding",
            agent="bridge",
            frontend="none",
            skills=[],
            sandbox=SandboxInput.model_validate(snapshot()["configuration"]),
            roles=ProjectRoles(allowed_tools={"implementer": grants}),
        ),
        prepared_tools=tool_catalog({"execute_command": True, "trace_python": True}),
    )
    observed = []

    class Runner:
        async def execute(self, *args, **kwargs):
            observed.append(args[3])
            return ProcessResult(args[3], 7, "Fixture process observation; exact receipt intentionally unavailable")

    class Worker(ConfiguredProvider):
        async def execute(self, workspace, *args):
            assert (self.runtime_context.python_trace is not None) is enabled
            if enabled:
                result = await self.runtime_context.python_trace(recipes.PythonTraceRequest(script="product.py"))
                assert result["exit_code"] == 7 and result["receipt"] is None
            raise ProviderLimitError("Owned worker stopped after capability inspection", kind="quota")

    engine = Engine(store, tmp_path / "workspaces", lambda _: Worker(), journal=Journal(store))
    engine.sandbox = Runner()
    await engine.execute_run(run["id"])
    actual = await store.get_run(run["id"])
    assert actual["status"] == "paused", actual.get("error")
    assert len(observed) == int(enabled)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoke", "cancel"])
async def test_trace_rechecks_authority_and_cancellation_after_capacity_wait(store, tmp_path, change):
    from test_sandbox_admission_authority import queued_admission

    async def dispatch(engine, run, workspace):
        return await engine._trace_python(run["id"], run, workspace, recipes.PythonTraceRequest(script="workload.py"))

    await queued_admission(store, tmp_path, change, dispatch=dispatch, extra_grants=("trace_python",))
