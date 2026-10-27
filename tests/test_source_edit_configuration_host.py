# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Mandatory controls cannot be overridden by source-only mode or hidden in optional defaults."""

import importlib
import json
import os
import sys

import pytest
from source_edit_qualification_fixtures import api_configuration, register_fixture_profile


def metadata_cli(tmp_path, monkeypatch, *, response="valid", startup_delay=0):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private_roots = (tmp_path / "private",)
    config_path, requests_path = tmp_path / "config.json", tmp_path / "requests.json"
    executable = tmp_path / "metadata-fixture"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,os,select,sys,time\n"
        f"with open({str(requests_path.with_suffix('.pid'))!r}, 'w') as stream: stream.write(str(os.getpid()))\n"
        f"time.sleep({startup_delay!r})\n"
        "assert os.environ.get('CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED') == '1'\n"
        "assert sys.argv[1:4] == ['app-server', '--listen', 'stdio://']\n"
        "assert '--strict-config' in sys.argv\n"
        "assert 'approval_policy=\"never\"' in sys.argv\n"
        f"assert os.getcwd() == {str(workspace)!r}\n"
        "def receive():\n"
        "    data = bytearray()\n"
        "    while len(data) < 65536:\n"
        "        byte = os.read(0, 1)\n"
        "        assert byte, 'metadata stdin closed before replies'\n"
        "        if byte == b'\\n':\n"
        "            return json.loads(data)\n"
        "        data.extend(byte)\n"
        "    raise AssertionError('fixture request too large')\n"
        "requests = [receive()]\n"
        "assert requests[0]['id'] == 1 and requests[0]['method'] == 'initialize'\n"
        "assert requests[0]['params']['capabilities'] == {'experimentalApi': True}\n"
        "assert not select.select([0], [], [], 0.05)[0], 'requests preceded initialize reply'\n"
        f"response = {response!r}\n"
        "if response != 'valid':\n"
        f"    with open({str(requests_path)!r}, 'w') as stream: json.dump(requests, stream)\n"
        "    if response == 'error': print(json.dumps({'id': 1, 'error': {'message': 'PUBLIC_ERROR'}}), flush=True)\n"
        "    if response == 'out_of_order': print(json.dumps({'id': 2, 'result': {}}), flush=True)\n"
        "    if response == 'invalid': print(json.dumps({'id': 1, 'result': None}), flush=True)\n"
        "    sys.exit(0)\n"
        "print(json.dumps({'id': 1, 'result': {'userAgent': 'public-fixture'}}), flush=True)\n"
        "requests.append(receive())\n"
        "assert requests[1] == {'method': 'initialized', 'params': {}}\n"
        "requests.append(receive())\n"
        f"assert requests[2] == {{'id': 2, 'method': 'config/read', 'params': "
        f"{{'includeLayers': False, 'cwd': {str(workspace)!r}}}}}\n"
        "requests.append(receive())\n"
        "assert requests[3] == {'id': 3, 'method': 'configRequirements/read', 'params': {}}\n"
        "assert not select.select([0], [], [], 0.05)[0], 'metadata stdin closed before replies'\n"
        f"with open({str(config_path)!r}) as stream: config = json.load(stream)\n"
        "print(json.dumps({'id': 2, 'result': {'config': config}}), flush=True)\n"
        "print(json.dumps({'id': 3, 'result': {'requirements': None}}), flush=True)\n"
        "assert os.read(0, 1) == b'', 'unexpected metadata request'\n"
        f"with open({str(requests_path)!r}, 'w') as stream: json.dump(requests, stream)\n"
    )
    executable.chmod(0o700)
    profile = register_fixture_profile(monkeypatch, executable)
    config_path.write_text(json.dumps(api_configuration(profile, workspace, private_roots)))
    from firefly_forge.provider_executable_adoption import executable_identity

    return (
        profile,
        executable_identity(str(executable), tmp_path / "workspaces"),
        workspace,
        private_roots,
        requests_path,
    )


@pytest.mark.parametrize("parent_value", [None, "parent-value"])
async def test_metadata_child_disables_persisted_remote_control_without_parent_mutation(
    tmp_path, monkeypatch, parent_value
):
    from firefly_forge.source_edit_cli_policy import qualify_configuration

    key = "CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED"
    if parent_value is None:
        monkeypatch.delenv(key, raising=False)
    else:
        monkeypatch.setenv(key, parent_value)
    profile, identity, workspace, private_roots, requests_path = metadata_cli(tmp_path, monkeypatch)
    proof = await qualify_configuration(identity, 5, profile=profile, workspace=workspace, private_roots=private_roots)
    assert proof["version"] == 5 and proof["profile"]["profile_id"] == profile.profile_id
    assert "closed" not in proof and proof["managed_execution_conflict"] is False
    assert len(proof["permissions_digest"]) == 64
    assert [row["method"] for row in json.loads(requests_path.read_text())] == [
        "initialize",
        "initialized",
        "config/read",
        "configRequirements/read",
    ]
    assert os.environ.get(key) == parent_value


@pytest.mark.parametrize("response", ["missing", "error", "out_of_order", "invalid"])
async def test_metadata_handshake_refuses_incomplete_or_unsuccessful_initialization(tmp_path, monkeypatch, response):
    from firefly_forge.providers import ProviderError
    from firefly_forge.source_edit_cli_policy import qualify_configuration

    profile, identity, workspace, private_roots, requests_path = metadata_cli(tmp_path, monkeypatch, response=response)
    failure = ProviderError if response == "missing" else ValueError
    with pytest.raises(failure, match="timed out|metadata"):
        await qualify_configuration(identity, 1, profile=profile, workspace=workspace, private_roots=private_roots)
    assert [row["method"] for row in json.loads(requests_path.read_text())] == ["initialize"]


async def test_metadata_honors_configured_window_above_five_seconds(tmp_path, monkeypatch):
    from firefly_forge.source_edit_cli_policy import qualify_configuration

    profile, identity, workspace, private_roots, requests_path = metadata_cli(tmp_path, monkeypatch, startup_delay=5.2)
    proof = await qualify_configuration(identity, 10, profile=profile, workspace=workspace, private_roots=private_roots)
    assert proof["version"] == 5
    assert [row["method"] for row in json.loads(requests_path.read_text())] == [
        "initialize",
        "initialized",
        "config/read",
        "configRequirements/read",
    ]


async def test_metadata_preserves_short_caller_deadline_and_stops_owned_child(tmp_path, monkeypatch):
    from firefly_forge.providers import ProviderError
    from firefly_forge.source_edit_cli_policy import qualify_configuration

    profile, identity, workspace, private_roots, requests_path = metadata_cli(tmp_path, monkeypatch, startup_delay=1)
    with pytest.raises(ProviderError, match="timed out after 0.15 seconds"):
        await qualify_configuration(identity, 0.15, profile=profile, workspace=workspace, private_roots=private_roots)
    assert not requests_path.exists()
    pid_file = requests_path.with_suffix(".pid")
    if pid_file.exists():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)


@pytest.mark.parametrize(
    "timeout",
    [0, -1, float("nan"), float("inf"), -float("inf"), True, False, "10", None, 1 + 0j, 10**1000],
    ids=[
        "zero",
        "negative",
        "nan",
        "infinite",
        "negative-infinite",
        "true",
        "false",
        "string",
        "none",
        "complex",
        "overflow",
    ],
)
async def test_invalid_metadata_deadline_is_rejected_before_process_dispatch(tmp_path, monkeypatch, timeout):
    from firefly_forge import providers
    from firefly_forge.source_edit_cli_policy import qualify_configuration

    profile, identity, workspace, private_roots, requests_path = metadata_cli(tmp_path, monkeypatch)
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Invalid metadata deadlines cannot dispatch a native process")

    monkeypatch.setattr(providers, "run_process", forbidden)
    with pytest.raises(ValueError, match="finite positive"):
        await qualify_configuration(
            identity, timeout, profile=profile, workspace=workspace, private_roots=private_roots
        )
    assert calls == [] and not requests_path.exists()


def project(config, requirements, profile, workspace, private_roots):
    module = importlib.import_module("firefly_forge.source_edit_cli_policy")
    assert hasattr(module, "configuration_projection")
    return module.configuration_projection(
        config, requirements, profile=profile, workspace=workspace, private_roots=private_roots
    )


@pytest.mark.parametrize(
    "agents", [None, {}, [], {"enabled": True}, {"enabled": 0}, {"enabled": False, "future": True}]
)
def test_agents_metadata_must_be_explicitly_disabled_without_unknown_controls(tmp_path, monkeypatch, agents):
    profile, _identity, workspace, private_roots, _requests = metadata_cli(tmp_path, monkeypatch)
    config = api_configuration(profile, workspace, private_roots)
    with pytest.raises(ValueError):
        project(config | {"agents": agents}, None, profile, workspace, private_roots)


def test_agents_disabled_projection_has_a_new_metadata_contract(tmp_path, monkeypatch):
    profile, _identity, workspace, private_roots, _requests = metadata_cli(tmp_path, monkeypatch)
    proof = project(api_configuration(profile, workspace, private_roots), None, profile, workspace, private_roots)
    assert proof["version"] == 5


def serialized_disabled_agents():
    return {
        "enabled": False,
        "max_concurrent_threads_per_session": None,
        "max_depth": None,
        "default_subagent_model": None,
        "default_subagent_reasoning_effort": None,
        "job_max_runtime_seconds": None,
        "interrupt_message": None,
    }


def test_serialized_agents_null_options_preserve_exact_disabled_contract(tmp_path, monkeypatch):
    profile, _identity, workspace, private_roots, _requests = metadata_cli(tmp_path, monkeypatch)
    config = api_configuration(profile, workspace, private_roots)
    proof = project(config | {"agents": serialized_disabled_agents()}, None, profile, workspace, private_roots)
    assert proof["version"] == 5


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", True),
        ("enabled", 0),
        ("enabled", None),
        ("enabled", "false"),
        ("max_concurrent_threads_per_session", 1),
        ("max_depth", 1),
        ("default_subagent_model", "public-unqualified-model"),
        ("default_subagent_reasoning_effort", "high"),
        ("job_max_runtime_seconds", 1),
        ("interrupt_message", False),
        ("researcher", {"description": "public role"}),
        ("max_threads", None),
        ("future", None),
    ],
)
def test_serialized_agents_reject_active_malformed_and_unknown_fields(tmp_path, monkeypatch, field, value):
    profile, _identity, workspace, private_roots, _requests = metadata_cli(tmp_path, monkeypatch)
    config = api_configuration(profile, workspace, private_roots)
    with pytest.raises(ValueError):
        project(
            config | {"agents": serialized_disabled_agents() | {field: value}}, None, profile, workspace, private_roots
        )


@pytest.mark.parametrize("field", tuple(serialized_disabled_agents()))
def test_serialized_agents_require_every_exact_option(tmp_path, monkeypatch, field):
    profile, _identity, workspace, private_roots, _requests = metadata_cli(tmp_path, monkeypatch)
    config = api_configuration(profile, workspace, private_roots)
    agents = serialized_disabled_agents()
    del agents[field]
    with pytest.raises(ValueError):
        project(config | {"agents": agents}, None, profile, workspace, private_roots)


@pytest.mark.parametrize("path", [None, "/unqualified/model-catalog.json"])
def test_model_catalog_path_is_part_of_native_configuration_admission(tmp_path, monkeypatch, path):
    profile, _identity, workspace, private_roots, _requests = metadata_cli(tmp_path, monkeypatch)
    config = api_configuration(profile, workspace, private_roots)
    with pytest.raises(ValueError):
        project(config | {"model_catalog_json": path}, None, profile, workspace, private_roots)


@pytest.mark.parametrize("boundary", ["metadata", "dispatch"])
async def test_catalog_drift_refuses_before_any_native_process(tmp_path, monkeypatch, boundary):
    from firefly_forge import providers, source_edit_model_catalog
    from firefly_forge.coding_check_transport import CodingCheckServer
    from firefly_forge.source_edit_cli_policy import qualify_configuration

    profile, identity, workspace, private_roots, _requests = metadata_cli(tmp_path, monkeypatch)
    source_edit_model_catalog.CATALOG_PATH.write_text("{}")
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("A changed catalog cannot reach native process launch")

    monkeypatch.setattr(providers, "run_process", forbidden)
    with pytest.raises(ValueError, match="qualified"):
        if boundary == "metadata":
            await qualify_configuration(identity, 5, profile=profile, workspace=workspace, private_roots=private_roots)
        else:

            async def reader(_request):
                raise AssertionError("A changed catalog cannot reach source readers")

            async def emit(*args):
                return None

            import time

            server = CodingCheckServer(None, source_callback=reader, expires_at_ms=int(time.time() * 1000) + 30000)
            server.endpoint = "http://127.0.0.1:19433/mcp"
            provider = providers.CodexProvider(
                {"FORGE_CODEX_EXECUTABLE": identity["path"], "FORGE_CODEX_MODEL": profile.model}
            )
            provider.configure_source_edits(server, "Public fixture")
            await provider.execute(workspace, "Public fixture", "pyfly", "none", emit)
    assert calls == []


def test_managed_hook_or_enabled_execution_feature_refuses_source_mode(tmp_path, monkeypatch):
    from firefly_forge.source_edit_cli_policy import DISABLED_FEATURES

    executable = tmp_path / "public-cli"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    profile = register_fixture_profile(monkeypatch, executable)
    workspace, private_roots = tmp_path / "workspace", (tmp_path / "private",)
    workspace.mkdir()
    config = api_configuration(profile, workspace, private_roots)
    assert project(config, None, profile, workspace, private_roots)["profile"]["profile_id"] == profile.profile_id
    for changed, requirements in (
        (config, {"hooks": {"SessionStart": [{"command": "must-run"}]}}),
        (config, {"featureRequirements": {"hooks": True}}),
        (config | {"features": config["features"] | {"shell_tool": True}}, None),
        (config | {"features": config["features"] | {"code_mode": True}}, None),
        *[
            (config | {"features": config["features"] | {key: True}}, None)
            for key in ("view_image", "deferred_executor", "token_budget", "sleep_tool", "send_message_to_user_async")
        ],
        (
            config
            | {"tools": {"update_plan": {"enabled": True}, "experimental_request_user_input": {"enabled": False}}},
            None,
        ),
        (
            config
            | {"tools": {"update_plan": {"enabled": False}, "experimental_request_user_input": {"enabled": True}}},
            None,
        ),
        (config | {"mcp_servers": {"other": {"command": "must-not-start"}}}, None),
        (config | {"notify": ["must-not-start"]}, None),
        (config | {"features": {key: False for key in DISABLED_FEATURES}}, None),
        (config | {"features": config["features"] | {"skip_host_skill_discovery": False}}, None),
        (config, {"featureRequirements": {"skip_host_skill_discovery": False}}),
        (config | {"features": config["features"] | {"future_exec_alias": True}}, None),
        (config | {"tools": config["tools"] | {"future_exec_alias": {"enabled": True}}}, None),
        (config | {"tools": {}}, None),
        (config | {"tools": {"update_plan": {"enabled": False}}}, None),
    ):
        with pytest.raises(ValueError):
            project(changed, requirements, profile, workspace, private_roots)


@pytest.mark.parametrize(
    "field,value",
    [
        ("skills", None),
        ("skills", []),
        ("skills", {"include_instructions": False, "bundled": None}),
        ("skills", {"include_instructions": False, "bundled": []}),
        ("cloud", None),
        ("cloud", {"skills": None}),
        ("cloud", {"skills": []}),
        ("analytics", []),
        ("feedback", "x"),
        ("otel", None),
    ],
)
def test_malformed_nested_native_configuration_refuses_safely(tmp_path, monkeypatch, field, value):
    profile, _identity, workspace, private_roots, _requests = metadata_cli(tmp_path, monkeypatch)
    config = api_configuration(profile, workspace, private_roots)
    with pytest.raises(ValueError):
        project(config | {field: value}, None, profile, workspace, private_roots)


@pytest.mark.parametrize("value", [[], 0, False, "x", ["hooks"], 1])
def test_malformed_managed_feature_requirements_refuse_safely(tmp_path, monkeypatch, value):
    profile, _identity, workspace, private_roots, _requests = metadata_cli(tmp_path, monkeypatch)
    config = api_configuration(profile, workspace, private_roots)
    with pytest.raises(ValueError):
        project(config, {"featureRequirements": value}, profile, workspace, private_roots)
