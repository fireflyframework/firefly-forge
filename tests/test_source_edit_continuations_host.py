# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit source-only authority never turns observed commands into spare capacity."""
# ruff: noqa: F811

import copy
import importlib
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
from source_edit_qualification_fixtures import metadata_proof, register_fixture_profile
from test_cli_command_budget_guard import archive, transcript
from test_workspace_dependency_reconciliation import legacy_workspace  # noqa: F401
from test_workspace_reconciliation_journal import environment_journal  # noqa: F401
from test_workspace_reconciliation_service import implementation, reconciliation_service  # noqa: F401

from firefly_forge.configuration import ProviderConfiguration
from firefly_forge.journal import digest
from firefly_forge.sandbox_models import capture_sandbox
from firefly_forge.tool_broker import tool_policy_snapshot
from firefly_forge.verification_models import verification_scope


@pytest.fixture(autouse=True)
def controlled_metadata(monkeypatch):
    from firefly_forge import source_edit_cli_policy

    async def qualified(executable, timeout, *, profile, workspace, private_roots):
        return metadata_proof(profile, workspace, private_roots)

    monkeypatch.setattr(source_edit_cli_policy, "qualify_configuration", qualified)


async def source_environment(env, tmp_path, monkeypatch):
    env = implementation(env)
    executable = tmp_path / "owned-official-cli-fixture"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    register_fixture_profile(monkeypatch, executable)
    env.store.configuration.update(
        "codex", ProviderConfiguration(values={"executable": str(executable), "model": "fixture-model"})
    )
    env.store.run.data.update(env.store.configuration.snapshot("codex"))
    roles = {
        "roles": [
            {
                "id": "implementer",
                "enabled": True,
                "stages": ["implement"],
                "allowed_tools": ["list_files", "read_file", "search_text", "write_file"],
            }
        ]
    }
    env.store.run.data["role_snapshot"] = {**roles, "digest": digest(roles)}
    env.store.run.data["tool_policy_snapshot"] = tool_policy_snapshot(env.store.configuration)
    env.store.run.data["sandbox_snapshot"] = capture_sandbox({"mode": "host", "command_limit": 35})
    env.store.run.data["commands"] *= 7
    old = [copy.deepcopy(row.data) for row in env.journal.records.rows]
    old[0]["payload"].update(
        commands_digest=digest({"commands": env.store.run.data["commands"]}),
        scope_digest=verification_scope(env.store.run.data),
        provider_digest=env.module.verification_provider(env.store, env.store.run.data)
        if hasattr(env.module, "verification_provider")
        else old[0]["payload"]["provider_digest"],
    )
    from firefly_forge.verification_models import verification_provider

    old[0]["payload"]["provider_digest"] = verification_provider(env.store, env.store.run.data)
    env.journal.records.rows.clear()
    for entry in old:
        await env.journal._append(
            env.root.name, entry["task_id"], entry["idempotency_key"], entry["kind"], entry["payload"]
        )
    archive(env.store, transcript(21))

    async def executions(run_id):
        return [
            {
                "execution_id": UUID(int=n).hex,
                "effects": {"start": {"status": "done", "evidence": {"outcome": "completed"}}},
            }
            for n in range(1, 8)
        ]

    env.journal.sandbox_executions = executions
    return env


def source_request(candidate):
    assert importlib.util.find_spec("firefly_forge.source_edit_models") is not None
    from firefly_forge.source_edit_models import SourceEditContinuationInput

    return SourceEditContinuationInput(
        version=1,
        request_id="e" * 32,
        candidate_digest=candidate["digest"],
        acknowledge_environment_only=True,
        acknowledge_resume_existing_task=True,
        acknowledge_existing_read_scope=True,
        acknowledge_no_commands=True,
    )


async def test_source_receipt_reads_original_journal_inside_native_unit(reconciliation_service, tmp_path, monkeypatch):
    from firefly_forge.source_edit_continuations import source_edit_receipt

    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    expected = await env.service.activate_source_edit(
        env.root.name, source_request(await env.service.source_candidate(env.root.name))
    )
    before = copy.deepcopy([row.data for row in env.journal.records.rows])
    unit, find = env.store._unit, env.journal.records.find_for_run
    depth, observed = 0, []

    @asynccontextmanager
    async def native_unit(**kwargs):
        nonlocal depth
        async with unit(**kwargs) as session:
            depth += 1
            try:
                yield session
            finally:
                depth -= 1

    async def current_journal(*args, **kwargs):
        assert depth > 0, "The journal repository requires its caller's native transaction"
        observed.append(depth)
        return await find(*args, **kwargs)

    monkeypatch.setattr(env.store, "_unit", native_unit)
    monkeypatch.setattr(env.journal.records, "find_for_run", current_journal)
    assert await source_edit_receipt(env.recoveries, env.store.run.data) == expected
    assert observed and depth == 0
    assert [row.data for row in env.journal.records.rows] == before


async def test_explicit_source_candidate_preserves_actual_shared_final_reserve(
    reconciliation_service, tmp_path, monkeypatch
):  # noqa: F811
    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Unmetered"):
        await env.service.candidate(env.root.name)
    candidate = await env.service.source_candidate(env.root.name)
    assert candidate["source_edit"]["tools"] == ["list_files", "read_file", "search_text"]
    assert candidate["source_edit"]["native_utilities"] == [
        "clock.curr_time",
        "list_mcp_resources",
        "list_mcp_resource_templates",
        "read_mcp_resource",
    ]
    assert candidate["source_edit"]["native_resources"] == {
        "server": "forge_coding",
        "resources": 0,
        "templates": 0,
        "read": "not_found",
    }
    assert candidate["source_edit"]["cli_commands"] == 21
    assert candidate["budget"]["remaining_commands"] == 7
    assert candidate["budget"]["final_checks"] == 7
    assert candidate["source_edit"]["interactive_commands"] == 0
    before = copy.deepcopy(env.store.run.data)
    body = source_request(candidate)
    receipt = await env.service.activate_source_edit(env.root.name, body)
    assert receipt == await env.service.get(env.root.name, body.request_id)
    assert await env.service.activate_source_edit(env.root.name, body) == receipt
    assert env.task.attempt == 2 and env.store.run.status == "queued"
    for key in ("sandbox_snapshot", "commands", "role_snapshot", "mcp_snapshot", "config_revision"):
        assert env.store.run.data.get(key) == before.get(key)
    assert env.store.run.data["source_edit_continuation"]["request_id"] == body.request_id


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 1.0),
        ("acknowledge_no_commands", 1),
        ("acknowledge_existing_read_scope", False),
        ("qualification", {"closed": True}),
        ("closed", True),
    ],
)
def test_source_activation_requires_exact_version_and_acknowledgements(field, value):
    candidate = {"digest": "a" * 64}
    body = source_request(candidate).model_dump()
    body[field] = value
    from firefly_forge.source_edit_models import SourceEditContinuationInput

    with pytest.raises(ValueError):
        SourceEditContinuationInput.model_validate(body)


async def test_source_activation_revocation_during_credential_wait_has_no_intent_or_effect(
    reconciliation_service, tmp_path, monkeypatch
):  # noqa: F811
    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    candidate = await env.service.source_candidate(env.root.name)
    before = (env.root / ".git/index").read_bytes()

    async def revoke(value):
        env.identity.enabled = False

    env.store.admit_credentials = revoke
    from firefly_forge.project_access import AccessDenied

    with pytest.raises(AccessDenied):
        await env.service.activate_source_edit(env.root.name, source_request(candidate))
    assert (env.root / ".git/index").read_bytes() == before
    assert not any(row.data["kind"] == "workspace_reconciliation_intent" for row in env.journal.records.rows)


async def test_source_mode_archive_cut_accepts_new_noncommand_stream_but_never_new_shell(reconciliation_service):  # noqa: F811
    env = implementation(reconciliation_service)
    from firefly_forge import cli_command_budget

    assert hasattr(cli_command_budget, "retained_command_history")
    events = transcript(21)
    archive(env.store, events)
    observed = await cli_command_budget.retained_command_history(env.store, env.root.name)
    assert observed["cli_commands"] == 21
    archive(env.store, events + [{"type": "thread.started", "thread_id": "source-only"}])
    assert (
        await cli_command_budget.retained_command_history(env.store, env.root.name, archive_cut=observed["archive_cut"])
    ) == observed
    archive(env.store, events + transcript(1, "new-shell"))
    with pytest.raises(ValueError, match="command"):
        await cli_command_budget.retained_command_history(env.store, env.root.name, archive_cut=observed["archive_cut"])


async def test_source_reader_uses_confined_component_and_checks_drift_after_admission(
    reconciliation_service, tmp_path, monkeypatch
):  # noqa: F811
    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    receipt = await env.service.activate_source_edit(
        env.root.name, source_request(await env.service.source_candidate(env.root.name))
    )
    env.store.run.status = "developing"
    env.store.run.data["status"] = "developing"
    env.task.status = "running"
    from uuid import NAMESPACE_URL, uuid5

    key = uuid5(NAMESPACE_URL, f"forge:{env.root.name}:implement:2").hex
    from firefly_forge import source_edit_continuations as module

    assert hasattr(module, "SourceEditInvocation")

    # Only persistence is substituted here; native SQL broker/authority is tested separately.
    async def invoke(run_id, task, tool, request_id, arguments, callback, **kwargs):
        await kwargs["validate_admission"]()
        result = await callback()
        guard = await kwargs["validate_result"](result)
        guard()
        return result

    from types import SimpleNamespace

    env.recoveries.engine.tool_broker = SimpleNamespace(invoke=invoke)
    env.task.data = {"idempotency_key": key}
    runtime = module.SourceEditInvocation(env.recoveries, env.root.name, key)
    from firefly_forge.source_edit_models import SourceReadRequest

    result = await runtime.run(SourceReadRequest(tool="read_file", request_id="b" * 32, path="frontend/app.js"))
    assert result["content"] == "export const value = 1;\n"
    assert result["source_digest"] == receipt["completed_cut"]["source_digest"]
    for path in ("../outside", ".git/config", "frontend/node_modules/secret", ".env"):
        with pytest.raises(ValueError):
            await runtime.run(SourceReadRequest(tool="read_file", request_id="c" * 32, path=path))


async def test_sealed_source_activation_cannot_dispatch_through_plain_or_forged_cli(
    reconciliation_service, tmp_path, monkeypatch
):  # noqa: F811
    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    receipt = await env.service.activate_source_edit(
        env.root.name, source_request(await env.service.source_candidate(env.root.name))
    )
    from uuid import NAMESPACE_URL, uuid5

    key = uuid5(NAMESPACE_URL, f"forge:{env.root.name}:implement:2").hex
    env.task.status, env.task.data = "running", {"idempotency_key": key}
    env.store.run.status = "developing"
    env.store.run.data["status"] = "developing"
    await env.journal._append(env.root.name, "implement", key, "intent", {})
    before = len(env.journal.records.rows)
    with pytest.raises(ValueError):
        await env.recoveries.normal_dispatch(env.root.name, key)
    assert len(env.journal.records.rows) == before
    from firefly_forge import source_edit_cli_policy as policy

    assert hasattr(policy, "source_edit_launch")
    import time

    from firefly_forge.coding_check_transport import CodingCheckServer

    async def read(request):
        raise AssertionError("Launch guard performs no tool call")

    server = CodingCheckServer(None, source_callback=read, expires_at_ms=int(time.time() * 1000) + 30000)
    server.endpoint = "http://127.0.0.1:19433/mcp"
    for permissions_digest in (None, "0" * 64):
        with (
            policy.source_edit_launch(
                server, env.store.run.data["source_edit_continuation"], permissions_digest=permissions_digest
            ),
            pytest.raises(ValueError, match="sealed native CLI channel"),
        ):
            await env.recoveries.normal_dispatch(env.root.name, key)
        assert len(env.journal.records.rows) == before
    with policy.source_edit_launch(
        server,
        env.store.run.data["source_edit_continuation"],
        permissions_digest=receipt["candidate"]["source_edit"]["configuration_proof"]["permissions_digest"],
    ):
        await env.recoveries.normal_dispatch(env.root.name, key)
        await env.recoveries.assert_launch(env.root.name, key)
    assert len(env.journal.records.rows) == before + 1
    with pytest.raises(ValueError):
        await env.recoveries.assert_launch(env.root.name, key)


async def test_original_thirty_five_counts_retained_cli_at_every_native_final_effect(
    reconciliation_service, tmp_path, monkeypatch
):  # noqa: F811
    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    await env.service.activate_source_edit(
        env.root.name, source_request(await env.service.source_candidate(env.root.name))
    )
    from firefly_forge import source_edit_continuations as module

    assert hasattr(module, "assert_source_final_budget")
    records = [{"execution_id": UUID(int=i).hex} for i in range(1, 15)]

    async def executions(run_id):
        return records

    env.journal.sandbox_executions = executions
    with pytest.raises(ValueError, match="combined"):
        await module.assert_source_final_budget(env.store, env.journal, env.store.run.data, "verify", "f" * 32)
    # Exact known receipt is a readback, never an eighth execution.
    await module.assert_source_final_budget(
        env.store, env.journal, env.store.run.data, "verify", records[-1]["execution_id"]
    )
    records.pop()
    await module.assert_source_final_budget(env.store, env.journal, env.store.run.data, "verify", "f" * 32)
    with pytest.raises(ValueError, match="final"):
        await module.assert_source_final_budget(env.store, env.journal, env.store.run.data, "implement", "f" * 32)


async def test_source_change_before_first_cli_intent_cannot_use_old_activation(
    reconciliation_service, tmp_path, monkeypatch
):  # noqa: F811
    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    receipt = await env.service.activate_source_edit(
        env.root.name, source_request(await env.service.source_candidate(env.root.name))
    )
    import time
    from uuid import NAMESPACE_URL, uuid5

    from firefly_forge.coding_check_transport import CodingCheckServer
    from firefly_forge.source_edit_cli_policy import source_edit_launch

    key = uuid5(NAMESPACE_URL, f"forge:{env.root.name}:implement:2").hex
    env.task.status, env.task.data = "running", {"idempotency_key": key}
    env.store.run.status = "developing"
    env.store.run.data["status"] = "developing"
    await env.journal._append(env.root.name, "implement", key, "intent", {})
    (env.root / "frontend/app.js").write_text("export const value = 2;\n")

    async def unused(request):
        raise AssertionError("Admission has no source callback")

    server = CodingCheckServer(None, source_callback=unused, expires_at_ms=int(time.time() * 1000) + 30000)
    server.endpoint = "http://127.0.0.1:19433/mcp"
    before = len(env.journal.records.rows)
    with (
        source_edit_launch(
            server,
            env.store.run.data["source_edit_continuation"],
            permissions_digest=receipt["candidate"]["source_edit"]["configuration_proof"]["permissions_digest"],
        ),
        pytest.raises(ValueError, match="source"),
    ):
        await env.recoveries.normal_dispatch(env.root.name, key)
    assert len(env.journal.records.rows) == before
    assert receipt["candidate"]["previous_task"]["attempt"] == 2


async def test_source_change_during_final_metadata_admission_keeps_receipt_and_queue_held(
    reconciliation_service, tmp_path, monkeypatch
):  # noqa: F811
    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    body = source_request(await env.service.source_candidate(env.root.name))
    from firefly_forge import source_edit_cli_policy

    calls = 0

    async def qualify(executable, timeout, *, profile, workspace, private_roots):
        nonlocal calls
        calls += 1
        if calls == 3:
            (env.root / "frontend/app.js").write_text("export const value = 99;\n")
        return metadata_proof(profile, workspace, private_roots)

    monkeypatch.setattr(source_edit_cli_policy, "qualify_configuration", qualify)
    with pytest.raises(ValueError, match="source|authority"):
        await env.service.activate_source_edit(env.root.name, body)
    assert env.store.run.status == "failed" and env.task.status == "queued"
    kinds = [row.data["kind"] for row in env.journal.records.rows]
    assert "workspace_reconciliation_intent" in kinds
    assert "workspace_reconciliation_receipt" not in kinds
    assert "workspace_reconciliation_queued" not in kinds


async def test_expired_source_activation_cannot_spend_final_native_reserve(
    reconciliation_service, tmp_path, monkeypatch
):  # noqa: F811
    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    receipt = await env.service.activate_source_edit(
        env.root.name, source_request(await env.service.source_candidate(env.root.name))
    )
    from firefly_forge import source_edit_continuations as module

    monkeypatch.setattr(module, "clock_ms", lambda: receipt["expires_at_ms"])
    with pytest.raises(ValueError, match="deadline|expired"):
        await module.assert_source_final_budget(env.store, env.journal, env.store.run.data, "verify", "f" * 32)


async def test_source_candidate_keeps_original_verification_cut_during_metadata_wait(
    reconciliation_service, tmp_path, monkeypatch
):  # noqa: F811
    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    from firefly_forge import source_edit_cli_policy

    async def qualify(executable, timeout, *, profile, workspace, private_roots):
        (env.root / "frontend/app.js").write_text("export const value = 99;\n")
        return metadata_proof(profile, workspace, private_roots)

    monkeypatch.setattr(source_edit_cli_policy, "qualify_configuration", qualify)
    with pytest.raises(ValueError, match="source|cut"):
        await env.service.source_candidate(env.root.name)
    assert not any(row.data["kind"] == "workspace_reconciliation_intent" for row in env.journal.records.rows)


async def test_unqualified_build_refuses_before_metadata_or_intent(reconciliation_service, tmp_path, monkeypatch):
    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    from firefly_forge import source_edit_cli_policy, source_edit_qualification

    monkeypatch.setattr(source_edit_qualification, "_profiles", lambda: ())
    executable = tmp_path / "owned-official-cli-fixture"
    executable.write_text("#!/bin/sh\nprintf 'unqualified-codex/999.0.0'\n")

    async def forbidden(executable, timeout, **kwargs):
        raise AssertionError("Unqualified executable reached native metadata")

    monkeypatch.setattr(source_edit_cli_policy, "qualify_configuration", forbidden)
    with pytest.raises(ValueError, match="qualified"):
        await env.service.source_candidate(env.root.name)
    assert not any(row.data["kind"] == "workspace_reconciliation_intent" for row in env.journal.records.rows)


async def test_withdrawn_profile_refuses_activation_before_intent(reconciliation_service, tmp_path, monkeypatch):
    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    body = source_request(await env.service.source_candidate(env.root.name))
    from firefly_forge import source_edit_qualification

    monkeypatch.setattr(source_edit_qualification, "_profiles", lambda: ())
    before = copy.deepcopy(env.store.run.data)
    with pytest.raises(ValueError, match="qualified"):
        await env.service.activate_source_edit(env.root.name, body)
    assert env.store.run.data == before
    assert not any(row.data["kind"] == "workspace_reconciliation_intent" for row in env.journal.records.rows)


async def test_metadata_boolean_cannot_replace_registered_profile_proof(reconciliation_service, tmp_path, monkeypatch):
    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    from firefly_forge import source_edit_cli_policy

    async def unbound(executable, timeout, **kwargs):
        return {"version": 1, "closed": True, "managed_execution_conflict": False}

    monkeypatch.setattr(source_edit_cli_policy, "qualify_configuration", unbound)
    with pytest.raises(ValueError, match="profile-bound"):
        await env.service.source_candidate(env.root.name)
    assert not any(row.data["kind"] == "workspace_reconciliation_intent" for row in env.journal.records.rows)


@pytest.mark.parametrize("boundary", ["reader", "dispatch", "final"])
async def test_withdrawn_profile_refuses_runtime_without_new_metadata(
    reconciliation_service, tmp_path, monkeypatch, boundary
):
    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    await env.service.activate_source_edit(
        env.root.name, source_request(await env.service.source_candidate(env.root.name))
    )
    from firefly_forge import source_edit_qualification
    from firefly_forge.source_edit_continuations import (
        assert_source_edit_dispatch,
        assert_source_final_budget,
        source_edit_receipt,
    )

    monkeypatch.setattr(source_edit_qualification, "_profiles", lambda: ())
    before = len(env.journal.records.rows)
    with pytest.raises(ValueError, match="qualified"):
        if boundary == "reader":
            await source_edit_receipt(env.recoveries, env.store.run.data)
        elif boundary == "dispatch":
            await assert_source_edit_dispatch(env.recoveries, env.store.run.data, None)
        else:
            await assert_source_final_budget(env.store, env.journal, env.store.run.data, "verify", "f" * 32)
    assert len(env.journal.records.rows) == before


async def test_executable_change_during_metadata_cannot_seal_candidate(reconciliation_service, tmp_path, monkeypatch):
    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    from firefly_forge import source_edit_cli_policy

    async def swapped(executable, timeout, *, profile, workspace, private_roots):
        (tmp_path / "owned-official-cli-fixture").write_text("#!/bin/sh\nexit 99\n")
        return metadata_proof(profile, workspace, private_roots)

    monkeypatch.setattr(source_edit_cli_policy, "qualify_configuration", swapped)
    with pytest.raises(ValueError, match="qualified native build"):
        await env.service.source_candidate(env.root.name)
    assert not any(row.data["kind"] == "workspace_reconciliation_intent" for row in env.journal.records.rows)


@pytest.mark.parametrize("legacy_version", [3, 4])
async def test_legacy_metadata_cannot_authorize_the_new_native_tool_policy(
    reconciliation_service, tmp_path, monkeypatch, legacy_version
):
    env = await source_environment(reconciliation_service, tmp_path, monkeypatch)
    from firefly_forge import source_edit_cli_policy

    async def legacy(executable, timeout, *, profile, workspace, private_roots):
        proof = metadata_proof(profile, workspace, private_roots)
        return proof | {"version": legacy_version}

    monkeypatch.setattr(source_edit_cli_policy, "qualify_configuration", legacy)
    with pytest.raises(ValueError, match="configuration proof changed"):
        await env.service.source_candidate(env.root.name)
    assert not any(row.data["kind"] == "workspace_reconciliation_intent" for row in env.journal.records.rows)
