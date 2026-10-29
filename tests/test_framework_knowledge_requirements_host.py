# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Runtime constraints retain immutable upstream manifest provenance."""

import hashlib
import json

import pytest
from test_sandbox_command_ordinals_host import command_case as command_case


def source(framework, body):
    path = "pyproject.toml" if framework == "pyfly" else "composer.json"
    repository = (
        "fireflyframework/fireflyframework-pyfly" if framework == "pyfly" else "fireflyframework/fireflyframework-php"
    )
    return {
        "path": path,
        "sha256": hashlib.sha256(body).hexdigest(),
        "blob_oid": hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest(),
        "bytes": len(body),
        "source_uri": f"https://github.com/{repository}/blob/" + ("a" * 40) + "/" + path,
    }


def test_requirements_bind_actual_manifest_and_refuse_old_runtime():
    from firefly_forge.framework_knowledge_requirements import manifest_requirements, runtime_satisfies

    body = b'[project]\nname="pyfly"\nversion="26.10.11"\nrequires-python=">=3.12"\n'
    result = manifest_requirements("pyfly", "26.10.11", "a" * 40, source("pyfly", body), body)
    assert result["runtime_requirement"] == ">=3.12" and result["source_sha256"] == hashlib.sha256(body).hexdigest()
    assert runtime_satisfies(result, "3.13.13") and not runtime_satisfies(result, "3.11.9")
    body = json.dumps(
        {"name": "fireflyframework/larafly", "require": {"php": "^8.5.12", "illuminate/support": "^13.38"}}
    ).encode()
    result = manifest_requirements("larafly", "26.10.4", "a" * 40, source("larafly", body), body)
    assert runtime_satisfies(result, "8.5.13") and not runtime_satisfies(result, "8.3.31")
    assert result["dependencies"]["illuminate/support"] == "^13.38"
    with pytest.raises(ValueError, match="manifest|digest"):
        manifest_requirements("larafly", "26.10.4", "a" * 40, source("larafly", body), body + b" ")


def test_python_manifest_and_exact_lock_cannot_disagree_with_range():
    from firefly_forge.framework_knowledge_targets import manifest_target

    with pytest.raises(ValueError, match="conflict|satisfy"):
        manifest_target(
            "pyfly",
            {
                "pyproject.toml": b'[project]\ndependencies=["pyfly>=26.10.11"]',
                "uv.lock": b'[[package]]\nname="pyfly"\nversion="26.10.7"',
            },
        )


async def test_native_runtime_preflight_refuses_old_php_before_any_model(tmp_path):
    from types import SimpleNamespace

    from firefly_forge.framework_knowledge_conformance import attest_runtime
    from firefly_forge.framework_knowledge_requirements import manifest_requirements
    from firefly_forge.process import ProcessResult
    from firefly_forge.product_provenance import content_digest

    body = json.dumps(
        {"name": "fireflyframework/larafly", "require": {"php": "^8.5.12", "illuminate/support": "^13.38"}}
    ).encode()
    requirement = manifest_requirements("larafly", "26.10.4", "a" * 40, source("larafly", body), body)
    pin = {"framework": "larafly", "framework_version": "26.10.4", "runtime_requirements": requirement}
    pin["digest"] = content_digest(pin)
    calls = []

    class Sandbox:
        async def execute(self, *args, **kwargs):
            calls.append(args[3])
            return ProcessResult(args[3], 0, json.dumps({"runtime": "php", "version": "8.3.31"}))

    with pytest.raises(ValueError, match="runtime|PHP|prerequisite"):
        await attest_runtime(
            SimpleNamespace(sandbox=Sandbox()),
            {"id": "b" * 32, "framework_knowledge_pin": pin},
            tmp_path,
            tmp_path,
            "c" * 32,
        )
    assert len(calls) == 1 and calls[0][0] == "php"


def test_native_runtime_receipt_capture_supports_host_bind_without_archive_digest():
    from firefly_forge.sandbox import SandboxExecutor

    captured = {}
    SandboxExecutor._capture_diagnostic(
        captured.update,
        "a" * 32,
        {
            "request_digest": "b" * 64,
            "snapshot_digest": "c" * 64,
            "request": {"argv": ["php", "-v"]},
        },
    )
    assert captured["execution_id"] == "a" * 32 and captured["workspace_digest"] is None


def test_checkpoint_cut_binds_exact_framework_pin_and_runtime_observation():
    from firefly_forge.agentic_checkpoint_invocation import _cut, invocation_cut
    from firefly_forge.product_provenance import content_digest

    legacy = {"agent": "bridge", "config_revision": "a" * 32}
    frozen_names = (
        "agent",
        "backend",
        "frontend",
        "role_snapshot",
        "policy_snapshot",
        "model_selection",
        "knowledge_context_digest",
        "campaign_context",
        "design_snapshot",
        "branding_snapshot",
        "product_definition",
    )
    expected = content_digest(
        {"bridge": _cut(legacy, "config_revision"), "frozen": {name: legacy.get(name) for name in frozen_names}}
    )
    original = invocation_cut(legacy)
    assert original == expected
    marked = {
        **legacy,
        "framework_knowledge_request": {"framework": "pyfly"},
        "framework_knowledge_pin": {"digest": "b" * 64},
        "framework_runtime_attestation": {"event_digest": "c" * 64},
    }
    assert invocation_cut(marked) != original
    changed = {**marked, "framework_runtime_attestation": {"event_digest": "d" * 64}}
    assert invocation_cut(changed) != invocation_cut(marked)


async def test_actual_host_executor_captures_receipt_after_known_cleanup(command_case):
    case = command_case
    captured = {}
    result = await case.fresh().execute(
        case.run_id, case.workspace, case.workspace, ["fixture-runtime"], 2, "verify", _receipt_sink=captured.update
    )
    assert result.exit_code == 0 and captured["execution_id"]
    rows = await case.journal.sandbox_executions(case.run_id)
    assert rows[0]["execution_id"] == captured["execution_id"] and rows[0]["status"] == "removed"
    assert rows[0]["effects"]["cleanup"]["status"] == "done"
