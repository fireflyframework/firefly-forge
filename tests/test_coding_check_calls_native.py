# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real native runner receipts and request settlement; no external model or network."""

import asyncio
from uuid import uuid4

import pytest
from test_coding_check_grants_native import granted
from test_provider_recovery_allowance_native import allowance_request, failed_fourth
from worker_reply_fixtures import authenticated_operator

from firefly_forge.coding_check_models import CodingCheckRevocationInput, ConfiguredCheckRequest
from firefly_forge.coding_check_runtime import ConfiguredCheckInvocation
from firefly_forge.provider_recovery_allowances import ProviderRecoveryAllowanceInput
from firefly_forge.sandbox_models import SandboxInput
from firefly_forge.tool_broker import ToolInvocationBroker, ToolReconciliationRequired


async def invocation(store, tmp_path, monkeypatch):
    create_run = store.create_run

    async def bounded_run(body):
        return await create_run(body.model_copy(update={"sandbox": SandboxInput(command_limit=16)}))

    monkeypatch.setattr(store, "create_run", bounded_run)
    env = await failed_fourth(store, tmp_path, monkeypatch)
    run_id = env.run["id"]
    service, request, _ = await granted(env, run_id)
    body = allowance_request(
        await env.allowances.candidate(run_id, "codex", adopt_current_executable=True),
        request_id=request.recovery_request_id,
    ).model_dump()
    body["coding_check_grant_id"] = request.request_id
    await env.allowances.reserve(run_id, ProviderRecoveryAllowanceInput.model_validate(body))
    key = (await store.get_run(run_id))["provider_recovery"]["task_key"]
    await env.recovery.dispatch(run_id, key)
    await store.update(run_id, status="developing")
    env.engine.tool_broker = ToolInvocationBroker(store, env.journal, tmp_path / "private")
    return env, ConfiguredCheckInvocation(service, run_id, key)


async def test_native_coding_check_same_request_reads_original_receipt_and_source_change_cannot_reuse_success(
    store, tmp_path, monkeypatch
):
    async with authenticated_operator(store):
        env, runtime = await invocation(store, tmp_path, monkeypatch)
        request = ConfiguredCheckRequest(command_index=0, request_id=uuid4().hex)
        before = len(env.backend_calls)
        first, repeated = await asyncio.gather(runtime.run(request), runtime.run(request))
        assert first == repeated
        assert first["exit_code"] == 0 and len(first["sandbox_execution_ids"]) == 1
        assert len(env.backend_calls) == before + 1
        assert env.backend_calls[-1] == ["approved-check", "0"]
        assert await runtime.run(request) == first
        workspace = env.engine.workspaces / env.run["id"]
        (workspace / "partial.py").write_text("VALUE = 3\n")
        with pytest.raises(ValueError, match="source"):
            await runtime.run(request)
        assert len(env.backend_calls) == before + 1
        other = await runtime.run(ConfiguredCheckRequest(command_index=0, request_id=uuid4().hex))
        assert other["source_before"] != first["source_before"]
        assert len(env.backend_calls) == before + 2
        assert await runtime.run(ConfiguredCheckRequest(command_index=0, request_id=other["invocation_id"])) == other
        with pytest.raises(ValueError, match="budget"):
            await runtime.run(ConfiguredCheckRequest(command_index=6, request_id=uuid4().hex))
        assert len(await env.journal.sandbox_executions(env.run["id"])) == 9


async def test_native_unknown_coding_check_blocks_new_identity_without_replaying_runner(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        env, runtime = await invocation(store, tmp_path, monkeypatch)
        calls = []

        async def interrupted_after_admission(*args, **kwargs):
            calls.append(True)
            raise RuntimeError("Controlled unknown runner outcome")

        monkeypatch.setattr(env.engine, "_project_command", interrupted_after_admission)
        first = ConfiguredCheckRequest(command_index=0, request_id=uuid4().hex)
        with pytest.raises(ToolReconciliationRequired):
            await runtime.run(first)
        for request in (first, ConfiguredCheckRequest(command_index=6, request_id=uuid4().hex)):
            with pytest.raises(ToolReconciliationRequired):
                await runtime.run(request)
        assert calls == [True]
        rows = await env.engine.tool_broker.invocations(env.run["id"])
        assert len(rows) == 1 and rows[0]["status"] == "unknown"


@pytest.mark.parametrize("change", ["revoke", "identity", "source"])
async def test_native_configured_check_credential_wait_rechecks_current_authority_before_intent(
    store, tmp_path, monkeypatch, change
):
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.project_access import AccessDenied

    async with authenticated_operator(store) as actor:
        env, runtime = await invocation(store, tmp_path, monkeypatch)
        original = store.admit_credentials
        changed = []

        async def during_wait(value):
            await original(value)
            if isinstance(value, dict) and value.get("tool") == "coding_configured_check" and not changed:
                changed.append(True)
                if change == "revoke":
                    state = (await store.get_run(env.run["id"]))["coding_check_grant"]
                    await runtime.grants.revoke(
                        env.run["id"],
                        CodingCheckRevocationInput(
                            request_id=uuid4().hex,
                            grant_id=state["grant_id"],
                            expected_revision=state["revision"],
                            reason="Revoke while credential admission is awaited.",
                        ),
                    )
                elif change == "identity":
                    async with store._unit():
                        user = await ForgeIdentityUserRepository().find_by_id(actor.id)
                        user.enabled = False
                        await ForgeIdentityUserRepository().save(user)
                else:
                    (env.engine.workspaces / env.run["id"] / "partial.py").write_text("VALUE = 9\n")

        monkeypatch.setattr(store, "admit_credentials", during_wait)
        before = len(env.backend_calls)
        with pytest.raises((ValueError, AccessDenied)):
            await runtime.run(ConfiguredCheckRequest(command_index=0, request_id=uuid4().hex))
        assert changed == [True] and len(env.backend_calls) == before
        assert await env.engine.tool_broker.invocations(env.run["id"]) == []


async def test_native_grant_expiry_after_effect_keeps_known_runner_receipt_but_tool_unknown_no_replay(
    store, tmp_path, monkeypatch
):
    from firefly_forge import coding_check_grants

    async with authenticated_operator(store):
        env, runtime = await invocation(store, tmp_path, monkeypatch)
        original = env.engine._project_command
        calls = []
        state = (await store.get_run(env.run["id"]))["coding_check_grant"]
        receipt = await runtime.grants.get(env.run["id"], state["grant_id"])

        async def expired_after_effect(*args, **kwargs):
            calls.append(True)
            result = await original(*args, **kwargs)
            monkeypatch.setattr(coding_check_grants, "clock_ms", lambda: receipt["expires_at_ms"] + 1)
            return result

        monkeypatch.setattr(env.engine, "_project_command", expired_after_effect)
        with pytest.raises(ToolReconciliationRequired):
            await runtime.run(ConfiguredCheckRequest(command_index=0, request_id=uuid4().hex))
        assert calls == [True] and len(await env.journal.sandbox_executions(env.run["id"])) == 8
        rows = await env.engine.tool_broker.invocations(env.run["id"])
        assert rows[0]["status"] == "unknown"
        with pytest.raises(ValueError, match="expired"):
            await runtime.run(ConfiguredCheckRequest(command_index=0, request_id=uuid4().hex))
        assert calls == [True]
