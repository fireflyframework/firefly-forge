# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Existing bounded execution remains separate from unavailable future pools."""

import copy
import importlib
import json
import sys

import pytest

from firefly_forge.sandbox_models import SandboxInput, capture_sandbox, validate_sandbox_snapshot

IMAGE = "python@sha256:" + "1" * 64
BINDING = {"profile_id": "local-projects", "revision": "2" * 64, "backend": "docker"}
LEGACY_HOST = {
    "configuration": {
        "browser": None,
        "command_limit": 50,
        "context": None,
        "cpus": 1.0,
        "image": None,
        "internet_search": False,
        "max_output_bytes": 1000000,
        "memory_mb": 1024,
        "mode": "host",
        "network": "none",
        "pids_limit": 128,
        "timeout": 300,
        "workspace_max_bytes": 67108864,
        "workspace_transport": "bind",
    },
    "digest": "c4ae48b0078b3bf49b54b870eb868344807cd25f102c919fed98268e921454b6",
    "effective_network": "host",
    "enforced_controls": ["timeout", "output"],
    "mode": "host",
    "reason": "Trusted host execution uses the service account; CPU, memory and network isolation are not provided",
}


def adapters():
    try:
        return importlib.import_module("firefly_forge.runner_backends")
    except ModuleNotFoundError as exc:
        raise AssertionError("The runner backend seam is missing") from exc


def future_configuration(**overrides):
    return SandboxInput.model_validate(
        {
            "mode": "docker",
            "image": IMAGE,
            "context": "unavailable-m1-context",
            "workspace_transport": "archive",
            "pool_binding": copy.deepcopy(BINDING),
            **overrides,
        }
    )


def test_legacy_default_snapshot_has_identical_json_and_digest_without_pool_binding():
    saved = json.dumps(LEGACY_HOST, sort_keys=True, separators=(",", ":"))
    assert json.dumps(capture_sandbox(None), sort_keys=True, separators=(",", ":")) == saved
    assert validate_sandbox_snapshot(LEGACY_HOST).model_dump().get("pool_binding") is None
    assert json.dumps(LEGACY_HOST, sort_keys=True, separators=(",", ":")) == saved


def test_future_binding_is_immutable_copied_and_included_in_snapshot_digest():
    body = copy.deepcopy(BINDING)
    config = future_configuration(pool_binding=body)
    snapshot = capture_sandbox(config)
    body["revision"] = "3" * 64
    assert snapshot["configuration"]["pool_binding"] == BINDING
    assert validate_sandbox_snapshot(snapshot).pool_binding.revision == BINDING["revision"]
    with pytest.raises(ValueError, match="frozen"):
        config.pool_binding.profile_id = "another-pool"
    altered = copy.deepcopy(snapshot)
    altered["configuration"]["pool_binding"]["revision"] = "3" * 64
    with pytest.raises(ValueError, match="digest"):
        validate_sandbox_snapshot(altered)


@pytest.mark.parametrize(
    "binding",
    [
        {**BINDING, "backend": "kubernetes"},
        {**BINDING, "profile_id": "../other"},
        {**BINDING, "revision": "latest"},
        {**BINDING, "endpoint": "https://private.invalid"},
    ],
)
def test_future_binding_rejects_unimplemented_backend_mutable_revision_and_transport_authority(binding):
    with pytest.raises(ValueError):
        future_configuration(pool_binding=binding)


@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": "host", "image": None, "context": None, "workspace_transport": "bind"},
        {"workspace_transport": "bind"},
        {"network": "egress"},
    ],
)
def test_future_binding_cannot_select_host_bind_sharing_or_broad_egress(overrides):
    with pytest.raises(ValueError, match="pool"):
        future_configuration(**overrides)


def test_future_pool_has_no_backend_selection_or_legacy_fallback():
    with pytest.raises(ValueError, match="pool.*not enabled"):
        adapters().RunnerBackends().select(future_configuration())


async def test_host_adapter_preserves_actual_exit_output_and_removes_provider_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "m1-private-provider-fixture")
    backend = adapters().RunnerBackends().select(SandboxInput())
    argv = [sys.executable, "-c", "import os; print(os.getenv('OPENAI_API_KEY')); raise SystemExit(7)"]
    result = await backend.run(SandboxInput(), argv, tmp_path, 5, 1024)
    assert result.exit_code == 7 and result.output == "None\n" and result.argv == argv


async def test_host_adapter_transfers_binary_input_without_text_decoding(tmp_path):
    backend = adapters().RunnerBackends().select(SandboxInput())
    data = b"fixture\x00\xff\xfe"
    code, output = await backend.run_bytes(
        SandboxInput(),
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        tmp_path,
        5,
        1024,
        input_data=data,
    )
    assert code == 0 and output == data


async def test_host_adapter_enforces_output_limit(tmp_path):
    backend = adapters().RunnerBackends().select(SandboxInput())
    with pytest.raises(RuntimeError, match="output limit"):
        await backend.run(SandboxInput(), [sys.executable, "-c", "print('x'*2048)"], tmp_path, 5, 1024)


async def test_host_adapter_terminates_timed_out_process(tmp_path):
    backend = adapters().RunnerBackends().select(SandboxInput())
    marker = tmp_path / "late-effect"
    with pytest.raises(TimeoutError):
        await backend.run(
            SandboxInput(),
            [
                sys.executable,
                "-c",
                f"import time; from pathlib import Path; time.sleep(30); Path({str(marker)!r}).touch()",
            ],
            tmp_path,
            1,
            1024,
        )
    assert not marker.exists()


async def test_adapters_reject_foreign_mode_before_execution(tmp_path):
    registry = adapters().RunnerBackends()
    host = SandboxInput()
    docker = SandboxInput(mode="docker", image=IMAGE, context="unavailable-m1-context")
    for backend, foreign in [(registry.select(host), docker), (registry.select(docker), host)]:
        with pytest.raises(ValueError, match="mode"):
            await backend.run(foreign, [sys.executable, "-c", "raise SystemExit(99)"], tmp_path, 5, 1024)


async def test_future_pool_probe_is_explicitly_unavailable_without_daemon_access(store):
    from firefly_forge.journal import Journal
    from firefly_forge.sandbox import SandboxExecutor

    readiness = await SandboxExecutor(store, Journal(store)).probe(capture_sandbox(future_configuration()))
    assert not readiness["available"] and readiness["status"] == "not_implemented"
    assert "pool" in readiness["reason"]


async def test_unavailable_pool_cannot_change_defaults_or_admit_a_run(store, tmp_path):
    from test_runner_configuration import service

    from firefly_forge.models import RepositoryInput
    from firefly_forge.runner_configuration import RunnerDefaults

    settings = await service(store, tmp_path)
    repo = await store.add_repository(RepositoryInput(name="Local", url=str(tmp_path)))
    before = await settings.configuration(repo["id"])
    with pytest.raises(ValueError, match="pool.*not enabled"):
        await settings.configure_repository(repo["id"], RunnerDefaults(sandbox=future_configuration()))
    with pytest.raises(ValueError, match="pool.*not enabled"):
        await settings.capture_run(repo["id"], future_configuration())
    assert await settings.configuration(repo["id"]) == before


async def test_internal_future_snapshot_cannot_dispatch_or_record_a_command_effect(store, tmp_path):
    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.sandbox import SandboxExecutor

    repo = await store.add_repository(RepositoryInput(name="Local", url=str(tmp_path)))
    run = await store.create_run(
        RunInput(repository_id=repo["id"], brief="Verify source", operation="test"),
        prepared_runner={
            "sandbox_snapshot": capture_sandbox(future_configuration()),
            "search_snapshot": None,
            "runner_config_revision": None,
        },
    )
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "verify")
    await store.acquire_ownership()
    marker = tmp_path / "unexpected-host-fallback"
    with pytest.raises(ValueError, match="pool.*not enabled"):
        await SandboxExecutor(store, journal).execute(
            run["id"],
            tmp_path,
            tmp_path,
            [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
            5,
            "verify",
        )
    assert not marker.exists() and await journal.sandbox_executions(run["id"]) == []


def test_native_runtime_injects_the_same_backend_registry_into_the_facade(tmp_path, postgres_schema):
    from starlette.testclient import TestClient

    from firefly_forge.api import create_application
    from firefly_forge.sandbox import SandboxExecutor

    registry_type = adapters().RunnerBackends
    app = create_application(tmp_path, "m1-local-owner-token-32-characters", role="api")
    with TestClient(app, client=("127.0.0.1", 50000)):
        context = app.state.runtime.context
        assert context.get_bean(SandboxExecutor).backends is context.get_bean(registry_type)


def test_captured_future_pool_does_not_claim_enforced_isolation():
    snapshot = capture_sandbox(future_configuration())
    assert snapshot["enforced_controls"] == []
    assert snapshot["effective_network"] == "unavailable"
    assert "not enabled" in snapshot["reason"]


async def test_retained_legacy_invocation_returns_receipt_without_rewrite_or_reexecution(store, tmp_path):
    import hashlib
    import uuid

    from firefly_forge.journal import Journal, digest
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.sandbox import SandboxExecutor

    legacy = copy.deepcopy(LEGACY_HOST)
    for field in ("workspace_transport", "workspace_max_bytes"):
        legacy["configuration"].pop(field)
    legacy["digest"] = hashlib.sha256(
        json.dumps(legacy["configuration"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    repo = await store.add_repository(RepositoryInput(name="Retained", url=str(tmp_path)))
    run = await store.create_run(
        RunInput(repository_id=repo["id"], brief="Read retained result", operation="test"),
        prepared_runner={"sandbox_snapshot": legacy, "search_snapshot": None, "runner_config_revision": None},
    )
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "verify")
    marker = tmp_path / "forbidden-replay"
    argv = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
    execution_id = uuid.uuid5(uuid.NAMESPACE_URL, f"forge:sandbox:{run['id']}:verify:1:1").hex
    request = {
        "browser": False,
        "output_limit": 1000000,
        "input_sha256": None,
        "argv": argv,
        "cwd": ".",
        "workspace": str(tmp_path),
        "configuration": legacy["configuration"],
        "timeout": 5,
        "git_root": None,
    }
    payload = {
        "parent_attempt": 1,
        "ordinal": 1,
        "container_name": "forge-sandbox-" + execution_id,
        "snapshot_digest": legacy["digest"],
        "request_digest": digest(request),
        "request": request,
    }
    for effect, evidence in [
        ("create", {"mode": "host"}),
        ("start", {"exit_code": 0, "output": "retained result\n"}),
        ("cleanup", {"mode": "host", "container": False}),
    ]:
        await journal.sandbox_effect_intent(run["id"], "verify", execution_id, effect=effect, payload=payload)
        await journal.sandbox_effect_receipt(run["id"], "verify", execution_id, effect=effect, evidence=evidence)
    retained = await journal.sandbox_executions(run["id"])
    await store.acquire_ownership()
    result = await SandboxExecutor(store, journal).execute(run["id"], tmp_path, tmp_path, argv, 5, "verify", ordinal=1)
    assert result.exit_code == 0 and result.output == "retained result\n"
    assert not marker.exists()
    assert await journal.sandbox_executions(run["id"]) == retained
    assert (await store.get_run(run["id"]))["sandbox_snapshot"] == legacy
    assert journal.verify_chain(await journal.entries(run["id"]))
