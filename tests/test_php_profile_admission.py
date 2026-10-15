# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The optional PHP recipe uses the existing native authority and command receipt boundary."""

import copy
from types import SimpleNamespace

import pytest

from firefly_forge import php_diagnostics as recipes
from firefly_forge.sandbox_models import capture_sandbox

IMAGE = "php@sha256:" + "a" * 64


def snapshot(**changes):
    return capture_sandbox(
        {
            "mode": "docker",
            "image": IMAGE,
            "context": "owned-test",
            "workspace_transport": "archive",
            **changes,
        }
    )


@pytest.mark.parametrize(
    "change",
    [
        {"network": "egress"},
        {"workspace_transport": "bind"},
        {"pool_binding": {"profile_id": "owned", "revision": "a" * 64}},
    ],
)
def test_php_policy_refuses_network_bind_and_pool(change):
    with pytest.raises(ValueError, match="PHP profiling"):
        recipes.profile_configuration(snapshot(**change))


def test_php_policy_keeps_snapshot_integrity_and_python_default_error_contract():
    from firefly_forge.diagnostic_recipes import profile_configuration

    assert recipes.profile_configuration(snapshot()).image == IMAGE
    with pytest.raises(ValueError, match="Python profiling"):
        profile_configuration(None)
    changed = copy.deepcopy(snapshot())
    changed["configuration"]["network"] = "egress"
    with pytest.raises(ValueError, match="digest"):
        recipes.profile_configuration(changed)


@pytest.mark.parametrize(
    "policy,available,role,expected",
    [
        (snapshot(), True, None, "available"),
        (snapshot(network="egress"), True, None, "not_configured"),
        (snapshot(workspace_transport="bind"), True, None, "not_configured"),
        (capture_sandbox(None), True, None, "not_configured"),
        (snapshot(), False, None, "not_configured"),
        (snapshot(), True, "api", "worker_unverified"),
    ],
)
async def test_php_readiness_uses_frozen_worker_boundary(policy, available, role, expected):
    from firefly_forge.runner_configuration import RunnerConfiguration

    async def probe(value):
        return {"available": available}

    runner = object.__new__(RunnerConfiguration)
    runner.store = SimpleNamespace(runtime_roles=SimpleNamespace(role=role) if role else None)
    runner.executor = SimpleNamespace(probe=probe)
    tools = {item["id"]: item for item in await runner.capabilities({"sandbox_snapshot": policy})}
    assert tools["profile_php"]["availability"] == expected


async def test_engine_php_observation_binds_original_source_image_and_command_receipt(store, tmp_path):
    import hashlib

    from firefly_forge.agent_roles import ProjectRoles
    from firefly_forge.engine import Engine
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.process import ProcessResult
    from firefly_forge.sandbox_models import SandboxInput
    from firefly_forge.workspace_tools import tool_catalog

    repo = await store.add_repository(RepositoryInput(name="Owned PHP", url="https://github.com/test/owned.git"))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            brief="Observe PHP fixture",
            sandbox=SandboxInput.model_validate(snapshot()["configuration"]),
            roles=ProjectRoles(allowed_tools={"implementer": ["execute_command", "profile_php"]}),
        ),
        prepared_tools=tool_catalog({"execute_command": True, "profile_php": True}),
    )
    workspace = tmp_path / "source"
    workspace.mkdir()
    source = b"<?php exit(7);\r\n"
    (workspace / "workload.php").write_bytes(source)
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
            return ProcessResult(args[3], 7, "FORGE_PHP_PROFILE_V1\n" + "x" * 17000)

    engine = Engine(store, tmp_path / "jobs")
    engine.sandbox = Runner()
    result = await engine._profile_php(run["id"], run, workspace, recipes.PHPProfileRequest(script="workload.php"))
    assert result["recipe"] == "forge-php-runtime-v1" and result["exit_code"] == 7
    assert result["observation_only"] is True and result["best_effort"] is True
    assert result["source_sha256"] == hashlib.sha256(source).hexdigest()
    assert result["image"] == IMAGE and result["argv"] == calls[0][0][3]
    assert result["receipt"]["start_key"] == "a" * 32 + ":start"
    assert result["receipt"]["cleanup_key"] == "a" * 32 + ":cleanup"
    assert result["output_truncated"] and len(result["output"]) == 16000
    assert calls[0][1]["_diagnostic_snapshot"] == run["sandbox_snapshot"]["digest"]
    assert (workspace / "workload.php").read_bytes() == source


async def test_php_without_executor_or_grants_refuses_instead_of_using_host(store, tmp_path):
    from firefly_forge.engine import Engine
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.sandbox_models import SandboxInput

    repo = await store.add_repository(RepositoryInput(name="Owned", url="https://github.com/test/owned.git"))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"], brief="Observe", sandbox=SandboxInput.model_validate(snapshot()["configuration"])
        )
    )
    (tmp_path / "workload.php").write_text("<?php file_put_contents('unexpected-write', 'bad');")
    with pytest.raises(ValueError, match="PHP profiling"):
        await Engine(store, tmp_path / "jobs")._profile_php(
            run["id"], run, tmp_path, recipes.PHPProfileRequest(script="workload.php")
        )
    assert not (tmp_path / "unexpected-write").exists()


@pytest.mark.parametrize("change", ["revoke", "cancel"])
async def test_php_rechecks_authority_after_capacity_wait_without_effects(store, tmp_path, change):
    from test_sandbox_admission_authority import queued_admission

    async def dispatch(engine, run, workspace):
        (workspace / "workload.php").write_text("<?php echo 'owned';")
        return await engine._profile_php(run["id"], run, workspace, recipes.PHPProfileRequest(script="workload.php"))

    await queued_admission(store, tmp_path, change, dispatch=dispatch, extra_grants=("profile_php",))
