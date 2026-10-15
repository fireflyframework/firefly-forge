# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native policy/WAL boundaries; Docker dispatch is separately qualified."""

import copy

import pytest

from firefly_forge import diagnostic_recipes as recipes
from firefly_forge.sandbox_models import capture_sandbox

IMAGE = "python@sha256:" + "a" * 64


def snapshot(**changes):
    return capture_sandbox(
        {"mode": "docker", "image": IMAGE, "context": "owned-test", "workspace_transport": "archive", **changes}
    )


@pytest.mark.parametrize(
    "change",
    [
        {"network": "egress"},
        {"workspace_transport": "bind"},
        {"pool_binding": {"profile_id": "owned", "revision": "a" * 64}},
    ],
)
def test_profile_policy_rejects_other_runner_contracts(change):
    with pytest.raises(ValueError, match="profiling"):
        recipes.profile_configuration(snapshot(**change))


def test_profile_policy_requires_the_exact_validated_frozen_snapshot():
    assert recipes.profile_configuration(snapshot()).image == IMAGE
    for invalid in [None, {}, capture_sandbox(None), {"mode": "pool"}]:
        with pytest.raises(ValueError):
            recipes.profile_configuration(invalid)
    changed = copy.deepcopy(snapshot())
    changed["configuration"]["network"] = "egress"
    with pytest.raises(ValueError, match="digest"):
        recipes.profile_configuration(changed)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [None, {"network": "egress"}, {"workspace_transport": "bind"}, {"timeout": 60}])
async def test_dispatch_rechecks_diagnostic_policy_before_any_intent(store, tmp_path, policy):
    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.sandbox import SandboxExecutor
    from firefly_forge.sandbox_models import SandboxInput

    await store.acquire_ownership()
    repo = await store.add_repository(RepositoryInput(name="owned", url="https://github.com/test/owned.git"))
    config = None if policy is None else SandboxInput.model_validate(snapshot(**policy)["configuration"])
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Observe owned workload", sandbox=config))
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "implement")
    runner = SandboxExecutor(store, journal)
    with pytest.raises(ValueError, match="profiling|diagnostic"):
        await runner.execute(
            run["id"],
            tmp_path,
            tmp_path,
            ["python3", "workload.py"],
            10,
            "implement",
            _diagnostic_snapshot=snapshot()["digest"],
        )
    assert await journal.sandbox_executions(run["id"]) == []
    assert journal.verify_chain(await journal.entries(run["id"]))


def test_observation_collector_failure_cannot_lose_a_known_result():
    from firefly_forge.sandbox import SandboxExecutor

    seen = []
    payload = {"request_digest": "a" * 64, "snapshot_digest": "b" * 64, "request": {"workspace_digest": "c" * 64}}

    def failing(value):
        seen.append(value)
        raise RuntimeError("Collector unavailable")

    SandboxExecutor._capture_diagnostic(failing, "d" * 32, payload)
    assert seen == [
        {
            "execution_id": "d" * 32,
            "request_digest": "a" * 64,
            "snapshot_digest": "b" * 64,
            "workspace_digest": "c" * 64,
            "start_key": "d" * 32 + ":start",
            "cleanup_key": "d" * 32 + ":cleanup",
        }
    ]


@pytest.mark.asyncio
async def test_engine_profile_observation_binds_exact_command_and_preserves_workload_exit(store, tmp_path):
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
            roles=ProjectRoles(allowed_tools={"implementer": ["execute_command", "profile_python"]}),
        ),
        prepared_tools=tool_catalog({"execute_command": True, "profile_python": True}),
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
            return ProcessResult(args[3], 7, "retained workload failure\nFORGE_CPROFILE_V1\n" + "x" * 17000)

    engine = Engine(store, tmp_path / "jobs")
    engine.sandbox = Runner()
    result = await engine._profile_python(run["id"], run, workspace, recipes.PythonProfileRequest(script="workload.py"))
    assert result["recipe"] == "forge-python-cprofile-v1"
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
async def test_profile_without_executor_or_grants_never_uses_host(store, tmp_path):
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
    with pytest.raises(ValueError, match="profiling|Profiling"):
        await engine._profile_python(run["id"], run, tmp_path, recipes.PythonProfileRequest(script="workload.py"))


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
async def test_worker_readiness_admits_only_the_profile_policy(policy, available, expected):
    from types import SimpleNamespace

    from firefly_forge.runner_configuration import RunnerConfiguration

    async def probe(value):
        return {"available": available}

    runner = object.__new__(RunnerConfiguration)
    runner.store = SimpleNamespace(runtime_roles=None)
    runner.executor = SimpleNamespace(probe=probe)
    result = {tool["id"]: tool for tool in await runner.capabilities({"sandbox_snapshot": policy})}
    assert result["profile_python"]["availability"] == expected


@pytest.mark.asyncio
async def test_api_readiness_remains_worker_unverified_for_a_profile_policy():
    from types import SimpleNamespace

    from firefly_forge.runner_configuration import RunnerConfiguration

    runner = object.__new__(RunnerConfiguration)
    runner.store = SimpleNamespace(runtime_roles=SimpleNamespace(role="api"))
    result = {tool["id"]: tool for tool in await runner.capabilities({"sandbox_snapshot": snapshot()})}
    assert result["profile_python"]["availability"] == "worker_unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize("collector_fails", [False, True])
async def test_retained_command_yields_exact_receipt_without_execution_or_wal_changes(store, tmp_path, collector_fails):
    import uuid

    from firefly_forge.journal import Journal, digest
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.sandbox import SandboxExecutor, pack_workspace
    from firefly_forge.sandbox_models import SandboxInput

    await store.acquire_ownership()
    repo = await store.add_repository(RepositoryInput(name="owned", url="https://github.com/test/owned.git"))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"], brief="Observe", sandbox=SandboxInput.model_validate(snapshot()["configuration"])
        )
    )
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    stage = await journal.intent(run["id"], "implement")
    attempt = (await journal.plan(run["id"]))["tasks"][1]["attempt"]
    execution_id = uuid.uuid5(uuid.NAMESPACE_URL, f"forge:sandbox:{run['id']}:implement:{attempt}:1").hex
    runner = SandboxExecutor(store, journal)
    runner.transfer_image = IMAGE
    argv = ["python3", "workload.py"]
    request = {
        "browser": False,
        "output_limit": 1000000,
        "input_sha256": None,
        "argv": argv,
        "cwd": ".",
        "workspace": str(tmp_path.resolve()),
        "configuration": snapshot()["configuration"],
        "timeout": 10,
        "git_root": None,
        "transfer_image": IMAGE,
        "workspace_digest": pack_workspace(tmp_path, 67108864)[1],
    }
    payload = {
        "parent_attempt": attempt,
        "ordinal": 1,
        "container_name": "forge-sandbox-" + execution_id,
        "snapshot_digest": snapshot()["digest"],
        "request_digest": digest(request),
        "request": request,
    }
    for effect in ["create", "start", "cleanup"]:
        await journal.sandbox_effect_intent(run["id"], "implement", execution_id, effect=effect, payload=payload)
        await journal.sandbox_effect_receipt(
            run["id"],
            "implement",
            execution_id,
            effect=effect,
            evidence={"exit_code": 7, "output": "retained nonzero observation"}
            if effect == "start"
            else {"container_absent": True},
        )
    before = await journal.entries(run["id"])
    observations = []

    def sink(binding):
        observations.append(binding)
        if collector_fails:
            raise RuntimeError("Diagnostic collector failure after a known result")

    result = await runner.execute(
        run["id"],
        tmp_path,
        tmp_path,
        argv,
        10,
        "implement",
        ordinal=1,
        _diagnostic_snapshot=snapshot()["digest"],
        _receipt_sink=sink,
    )
    assert result.exit_code == 7 and result.output == "retained nonzero observation"
    assert len(observations) == 1 and observations[0]["execution_id"] == execution_id
    assert observations[0]["request_digest"] == digest(request)
    assert observations[0]["workspace_digest"] == request["workspace_digest"]
    assert await journal.entries(run["id"]) == before
    assert journal.verify_chain(before)
    assert stage["disposition"] == "execute"


@pytest.mark.asyncio
async def test_current_actor_revocation_and_cancellation_prevent_profile_dispatch(store, tmp_path):
    import asyncio

    from test_project_access import principal, seed

    from firefly_forge.agent_roles import ProjectRoles
    from firefly_forge.engine import Engine
    from firefly_forge.models import RunInput
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
    from firefly_forge.sandbox_models import SandboxInput
    from firefly_forge.workspace_tools import tool_catalog

    repo, projects = await seed(store, tmp_path)
    actor = principal()
    acl = ProjectAccess(store)
    await acl.set_membership(projects[0]["id"], actor, "developer")
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        run = await store.create_run(
            RunInput(
                repository_id=repo["id"],
                project_id=projects[0]["id"],
                component_id="service",
                brief="Observe",
                agent="agentic",
                sandbox=SandboxInput.model_validate(snapshot()["configuration"]),
                roles=ProjectRoles(allowed_tools={"implementer": ["execute_command", "profile_python"]}),
            ),
            prepared_tools=tool_catalog({"execute_command": True, "profile_python": True}),
        )
    finally:
        CURRENT_PRINCIPAL.reset(token)
    workspace = tmp_path / "source"
    (workspace / "workload.py").write_text("print('owned')")

    class ForbiddenRunner:
        async def execute(self, *args, **kwargs):
            raise AssertionError("Revoked/cancelled work cannot dispatch")

    engine = Engine(store, tmp_path / "jobs")
    engine.sandbox = ForbiddenRunner()
    await acl.set_membership(projects[0]["id"], actor, None)
    with pytest.raises(AccessDenied):
        await engine._profile_python(run["id"], run, workspace, recipes.PythonProfileRequest(script="workload.py"))
    await acl.set_membership(projects[0]["id"], actor, "developer")
    await store.update(run["id"], cancel_request={"status": "requested"})
    with pytest.raises(asyncio.CancelledError):
        await engine._profile_python(run["id"], run, workspace, recipes.PythonProfileRequest(script="workload.py"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "grants,enabled",
    [(["execute_command"], False), (["profile_python"], False), (["execute_command", "profile_python"], True)],
)
async def test_engine_supplies_the_optional_profile_hook_to_the_native_worker(store, tmp_path, grants, enabled):
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
        prepared_tools=tool_catalog({"execute_command": True, "profile_python": True}),
    )
    observed = []

    class Runner:
        async def execute(self, *args, **kwargs):
            observed.append(args[3])
            return ProcessResult(args[3], 7, "Fixture process observation; exact receipt intentionally unavailable")

    class Worker(ConfiguredProvider):
        async def execute(self, workspace, *args):
            assert (self.runtime_context.python_profile is not None) is enabled
            if enabled:
                result = await self.runtime_context.python_profile(recipes.PythonProfileRequest(script="product.py"))
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
async def test_profile_rechecks_authority_and_cancellation_after_capacity_wait(store, tmp_path, change):
    from test_sandbox_admission_authority import queued_admission

    async def dispatch(engine, run, workspace):
        return await engine._profile_python(
            run["id"], run, workspace, recipes.PythonProfileRequest(script="workload.py")
        )

    await queued_admission(store, tmp_path, change, dispatch=dispatch, extra_grants=("profile_python",))
