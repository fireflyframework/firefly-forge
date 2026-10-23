# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The public native SQL fixture must complete the real restricted metadata handshake."""

import copy
from types import SimpleNamespace

import pytest
import test_source_edit_native as fixture

from firefly_forge import providers, source_edit_cli_policy
from firefly_forge.configuration import ConfigurationStore
from firefly_forge.provider_executable_adoption import executable_identity
from firefly_forge.source_edit_model_catalog import model_catalog_path
from firefly_forge.source_edit_qualification import qualified_profile


@pytest.fixture
async def native_cli(tmp_path, monkeypatch):
    class FixtureConstructed(Exception):
        pass

    async def before_database(*args, **kwargs):
        raise FixtureConstructed

    monkeypatch.setattr(fixture, "retained_environment_refusal", before_database)
    store = SimpleNamespace(configuration=ConfigurationStore(tmp_path / "fixture-config"))
    with pytest.raises(FixtureConstructed):
        await fixture.prepared(store, tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = executable_identity(str(tmp_path / "public-native-cli-fixture"), tmp_path / "workspaces")
    return identity, qualified_profile(identity, "fixture-model"), workspace, (tmp_path / "private",)


async def observe_metadata(native_cli, monkeypatch, *, tamper=None):
    executable, profile, workspace, private_roots = native_cli
    seen = {}
    run = providers.run_process
    project = source_edit_cli_policy.configuration_projection

    async def process(command, *args, **kwargs):
        command = list(command)
        if tamper == "enabled":
            command[command.index("agents.enabled=false")] = "agents.enabled=true"
        elif tamper == "optional":
            command += ["-c", 'agents.default_subagent_model="public-unqualified"']
        elif tamper == "catalog":
            index = next(i for i, value in enumerate(command) if value.startswith("model_catalog_json="))
            command[index] = 'model_catalog_json="public-unqualified-catalog.json"'
        result = await run(command, *args, **kwargs)
        seen["packet_ids"] = set(kwargs["stdin_protocol"].results)
        seen["exit"] = result.returncode
        return result

    def projection(config, requirements, **kwargs):
        seen["config"] = copy.deepcopy(config)
        return project(config, requirements, **kwargs)

    monkeypatch.setattr(providers, "run_process", process)
    monkeypatch.setattr(source_edit_cli_policy, "configuration_projection", projection)
    return seen, lambda: source_edit_cli_policy.qualify_configuration(
        executable, 3, profile=profile, workspace=workspace, private_roots=private_roots
    )


async def test_sql_fixture_real_handshake_projects_exact_agents_and_catalog(native_cli, monkeypatch):
    seen, qualify = await observe_metadata(native_cli, monkeypatch)
    proof = await qualify()
    assert proof["version"] == 5 and seen["packet_ids"] == {1, 2, 3} and seen["exit"] == 0
    assert seen["config"]["agents"] == {
        "enabled": False,
        "max_concurrent_threads_per_session": None,
        "max_depth": None,
        "default_subagent_model": None,
        "default_subagent_reasoning_effort": None,
        "job_max_runtime_seconds": None,
        "interrupt_message": None,
    }
    assert seen["config"]["model_catalog_json"] == str(model_catalog_path("fixture-model"))
    _executable, profile, workspace, private_roots = native_cli
    observed_config = copy.deepcopy(seen["config"])
    for field in ("agents", "model_catalog_json"):
        config = {key: value for key, value in observed_config.items() if key != field}
        with pytest.raises(ValueError):
            source_edit_cli_policy.configuration_projection(
                config, None, profile=profile, workspace=workspace, private_roots=private_roots
            )


@pytest.mark.parametrize("tamper", ["enabled", "optional", "catalog"])
async def test_sql_fixture_does_not_hide_unsafe_actual_argv(native_cli, monkeypatch, tamper):
    seen, qualify = await observe_metadata(native_cli, monkeypatch, tamper=tamper)
    with pytest.raises(ValueError):
        await qualify()
    assert seen["packet_ids"] == {1, 2, 3} and seen["exit"] == 0
    if tamper == "enabled":
        assert seen["config"]["agents"]["enabled"] is True
    elif tamper == "optional":
        assert seen["config"]["agents"]["default_subagent_model"] == "public-unqualified"
    else:
        assert seen["config"]["model_catalog_json"] == "public-unqualified-catalog.json"
