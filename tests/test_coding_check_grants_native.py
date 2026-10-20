# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native grant/allowance authority; external providers remain controlled fixtures."""

import asyncio
import copy
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from test_provider_recovery_allowance_native import allowance_request, failed_fourth
from worker_reply_fixtures import authenticated_operator

from firefly_forge.coding_check_grants import CodingCheckGrants
from firefly_forge.coding_check_models import CodingCheckGrantInput, CodingCheckRevocationInput
from firefly_forge.provider_recovery_allowances import ProviderRecoveryAllowanceInput


async def granted(env, run_id):
    service = CodingCheckGrants(env.recovery)
    env.engine.coding_checks = service
    future_id = uuid4().hex
    candidate = await service.candidate(run_id, future_id)
    request = CodingCheckGrantInput(
        request_id=uuid4().hex,
        recovery_request_id=future_id,
        candidate_digest=candidate["digest"],
        expected_revision=0,
        command_indices=[0, 6],
        call_limit=2,
        valid_seconds=300,
        reason="Explicitly reviewed configured checks through the native runner.",
    )
    receipt = await service.grant(run_id, request)
    return service, request, receipt


async def test_native_coding_check_grant_prebinds_future_allowance_without_dispatch_or_snapshot_mutation(
    store, tmp_path, monkeypatch
):
    async with authenticated_operator(store):
        env = await failed_fourth(store, tmp_path, monkeypatch)
        run_id = env.run["id"]
        before = await store.get_run(run_id)
        prefix = copy.deepcopy(await env.journal.entries(run_id))
        service, request, receipt = await granted(env, run_id)
        assert receipt == await service.grant(run_id, request) == await service.get(run_id, request.request_id)
        current = await store.get_run(run_id)
        for key in ("commands", "sandbox_snapshot", "role_snapshot", "config_revision", "repair_budget", "attempt"):
            assert current[key] == before[key]
        assert current["provider_recovery"] == before["provider_recovery"]
        entries = await env.journal.entries(run_id)
        assert entries[: len(prefix)] == prefix
        assert [entry["kind"] for entry in entries[len(prefix) :]] == ["coding_check_grant"]
        candidate = await env.allowances.candidate(run_id, "codex", adopt_current_executable=True)
        body = allowance_request(candidate, request_id=request.recovery_request_id).model_dump()
        body["coding_check_grant_id"] = request.request_id
        accepted = await env.allowances.reserve(run_id, ProviderRecoveryAllowanceInput.model_validate(body))
        assert accepted["request"]["coding_check_grant_id"] == request.request_id
        assert (await store.get_run(run_id))["provider_recovery"]["status"] == "reserved"


async def test_native_revoked_coding_check_grant_holds_reserved_attempt_without_cli_dispatch(
    store, tmp_path, monkeypatch
):
    async with authenticated_operator(store):
        env = await failed_fourth(store, tmp_path, monkeypatch)
        run_id = env.run["id"]
        service, request, _ = await granted(env, run_id)
        body = allowance_request(
            await env.allowances.candidate(run_id, "codex", adopt_current_executable=True),
            request_id=request.recovery_request_id,
        ).model_dump()
        body["coding_check_grant_id"] = request.request_id
        await env.allowances.reserve(run_id, ProviderRecoveryAllowanceInput.model_validate(body))
        revocation = CodingCheckRevocationInput(
            request_id=uuid4().hex,
            grant_id=request.request_id,
            expected_revision=1,
            reason="Revoke before the reserved invocation is launched.",
        )
        first = await service.revoke(run_id, revocation)
        assert first == await service.revoke(run_id, revocation)
        calls = []

        class UnreachableProvider:
            async def execute(self, *args):
                calls.append(True)
                raise AssertionError("A revoked coding-check grant must hold before the CLI")

        from firefly_forge import providers

        env.engine.configured_provider = True
        monkeypatch.setattr(providers, "get_provider", lambda *args: UnreachableProvider())
        before = await env.journal.entries(run_id)
        await asyncio.wait_for(env.engine.execute_run(run_id), 10)
        current = await store.get_run(run_id)
        assert calls == []
        assert current["status"] == "needs_reconciliation"
        assert current["provider_recovery"]["status"] == "reserved"
        assert not any(
            row["kind"] == "provider_dispatch_intent" for row in (await env.journal.entries(run_id))[len(before) :]
        )


async def test_native_cross_kind_grant_request_identity_collision_is_refused_before_mutation(
    store, tmp_path, monkeypatch
):
    async with authenticated_operator(store):
        env = await failed_fourth(store, tmp_path, monkeypatch)
        run_id = env.run["id"]
        service, request, receipt = await granted(env, run_id)
        prefix = await env.journal.entries(run_id)
        with pytest.raises(ValueError, match="identity"):
            await service.revoke(
                run_id,
                CodingCheckRevocationInput(
                    request_id=request.request_id,
                    grant_id=request.request_id,
                    expected_revision=1,
                    reason="A grant identity cannot also identify its revocation.",
                ),
            )
        assert await env.journal.entries(run_id) == prefix
        assert await service.get(run_id, request.request_id) == receipt
        revoke = CodingCheckRevocationInput(
            request_id=uuid4().hex,
            grant_id=request.request_id,
            expected_revision=1,
            reason="Explicitly revoke this grant.",
        )
        await service.revoke(run_id, revoke)
        candidate = await service.candidate(run_id, uuid4().hex)
        changed = request.model_copy(
            update={
                "request_id": revoke.request_id,
                "expected_revision": 2,
                "recovery_request_id": candidate["recovery_request_id"],
                "candidate_digest": candidate["digest"],
            }
        )
        prefix = await env.journal.entries(run_id)
        with pytest.raises(ValueError, match="identity"):
            await service.grant(run_id, changed)
        assert await env.journal.entries(run_id) == prefix


async def test_native_coding_channel_startup_failure_keeps_reserved_dispatch_unconsumed(store, tmp_path, monkeypatch):
    from firefly_forge import providers
    from firefly_forge.coding_check_transport import CodingCheckServer
    from firefly_forge.tool_broker import ToolInvocationBroker

    async with authenticated_operator(store):
        env = await failed_fourth(store, tmp_path, monkeypatch)
        run_id = env.run["id"]
        service, request, _ = await granted(env, run_id)
        body = allowance_request(
            await env.allowances.candidate(run_id, "codex", adopt_current_executable=True),
            request_id=request.recovery_request_id,
        ).model_dump()
        body["coding_check_grant_id"] = request.request_id
        await env.allowances.reserve(run_id, ProviderRecoveryAllowanceInput.model_validate(body))
        env.engine.tool_broker = ToolInvocationBroker(store, env.journal, tmp_path / "private")
        env.engine.configured_provider = True
        monkeypatch.setattr(providers, "get_provider", lambda _, settings: providers.CodexProvider(settings))
        startup, cli = [], []

        @asynccontextmanager
        async def unavailable(self):
            startup.append(True)
            raise OSError("Controlled port-bind failure before coding CLI launch")
            yield

        async def unreachable(*args, **kwargs):
            cli.append(True)
            raise AssertionError("No coding subprocess may start without its required channel")

        monkeypatch.setattr(CodingCheckServer, "running", unavailable)
        monkeypatch.setattr(providers, "run_process", unreachable)
        prefix = await env.journal.entries(run_id)
        await env.engine.execute_run(run_id)
        assert startup == [True] and cli == []
        run = await store.get_run(run_id)
        assert run["provider_recovery"]["status"] == "reserved" and run["status"] == "needs_reconciliation"
        assert not any(
            e["kind"] == "provider_dispatch_intent" for e in (await env.journal.entries(run_id))[len(prefix) :]
        )
