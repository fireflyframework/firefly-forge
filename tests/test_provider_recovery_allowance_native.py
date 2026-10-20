# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real PostgreSQL/WAL/Engine; coding providers and command runners are controlled fixtures."""

import asyncio
import copy
from uuid import UUID, uuid4

import pytest
from test_provider_recovery_native import legacy_failure
from worker_reply_fixtures import authenticated_operator

from firefly_forge.configuration import ProviderConfiguration
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.project_access import AccessDenied
from firefly_forge.provider_recovery_allowances import ProviderRecoveryAllowanceInput, ProviderRecoveryAllowances
from firefly_forge.provider_recovery_models import ProviderProcessOutcome, ProviderRecoveryAdmission
from firefly_forge.providers import AgentResult, ProviderError

OUTCOME = ProviderProcessOutcome(exit_code=-25, leader_waited=True, streams_drained=True, group_absent=True)


def allowance_request(candidate, **changes):
    return ProviderRecoveryAllowanceInput.model_validate(
        dict(
            request_id=uuid4().hex,
            candidate_digest=candidate["digest"],
            native_process_digest=candidate["native_process_digest"],
            agent="codex",
            reason="Reviewed the finite CLI signal mitigation against controlled local fixtures.",
            mitigation_evidence_digest="e" * 64,
            adopt_current_executable=True,
        )
        | changes
    )


async def failed_fourth(store, tmp_path, monkeypatch, *, unknown_fourth=False):
    env = await legacy_failure(store, tmp_path, monkeypatch)
    run_id = env.run["id"]
    async with store._unit():
        task = await env.journal._task(run_id, "implement")
    await env.recovery.record_process(run_id, task.data["idempotency_key"], OUTCOME)

    class FailedNativeProvider:
        async def execute(self, workspace, *args):
            (workspace / "retained-note.txt").write_text("Native controlled failure keeps partial source\n")
            error = ProviderError("Controlled native CLI failure; no inference was performed")
            attempt = (await store.get_run(run_id))["attempt"]
            error.process_outcome = (
                OUTCOME.model_copy(update={"group_absent": False}) if unknown_fourth and attempt == 4 else OUTCOME
            )
            raise error

    env.engine.provider_factory = lambda _: FailedNativeProvider()
    for attempt in (3, 4):
        candidate = await env.recovery.candidate(run_id, "codex")
        assert candidate["next_attempt"] == attempt
        await env.recovery.admit(
            run_id,
            ProviderRecoveryAdmission(
                request_id=uuid4().hex,
                candidate_digest=candidate["digest"],
                agent="codex",
                native_process_digest=candidate["native_process_digest"],
            ),
        )
        await env.engine.execute_run(run_id)
        expected = "needs_reconciliation" if unknown_fourth and attempt == 4 else "failed"
        assert (await store.get_run(run_id))["status"] == expected
    executable = tmp_path / "reviewed-fixture-cli"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    store.configuration.update("codex", ProviderConfiguration(values={"executable": str(executable)}))
    env.allowances = ProviderRecoveryAllowances(env.recovery)
    return env


async def test_native_final_allowance_engine_executes_once_preserves_grants_and_original_wal(
    store, tmp_path, monkeypatch
):
    async with authenticated_operator(store):
        env = await failed_fourth(store, tmp_path, monkeypatch)
        run_id = env.run["id"]
        before = await store.get_run(run_id)
        prefix = await env.journal.entries(run_id)
        with pytest.raises(ValueError, match="four-dispatch"):
            await env.recovery.candidate(run_id, "codex")
        body = allowance_request(await env.allowances.candidate(run_id, "codex", adopt_current_executable=True))
        first, repeated = await asyncio.wait_for(
            asyncio.gather(env.allowances.reserve(run_id, body), env.allowances.reserve(run_id, body)), 10
        )
        assert first == repeated == await env.allowances.get(run_id, body.request_id)
        calls = []

        class FinalProvider:
            async def execute(self, workspace, *args):
                calls.append(workspace)
                assert (
                    workspace / "retained-note.txt"
                ).read_text() == "Native controlled failure keeps partial source\n"
                (workspace / "partial.py").write_text("VALUE = 2\n")
                state = (await store.get_run(run_id))["provider_recovery"]
                return AgentResult(
                    "Controlled retained implementation",
                    "fixture",
                    {
                        "worker_feedback": dict(
                            id="final-native-feedback",
                            role_id="implementer",
                            task_id="implement",
                            attempt=5,
                            disposition="completed",
                            summary="Implementation reported; checks remain independent.",
                            evidence_paths=["partial.py"],
                            request=None,
                            protocol="native-provider-recovery-allowance-v1",
                            allowance_id=body.request_id,
                            allowance_digest=state["allowance_digest"],
                        )
                    },
                )

        from firefly_forge import providers

        env.engine.configured_provider = True
        monkeypatch.setattr(providers, "get_provider", lambda agent, settings: FinalProvider())
        await env.engine.execute_run(run_id)
        final = await store.get_run(run_id)
        assert final["status"] == "succeeded", final.get("error")
        assert len(calls) == 1 and final["attempt"] == 5
        assert final["provider_recovery"]["status"] == "completed"
        assert final["worker_feedback"]["attempt"] == 5
        for name in ("repair_budget", "verification_handoff", "config_revision", "worker_response"):
            assert final[name] == before[name]
        entries = await env.journal.entries(run_id)
        assert entries[: len(prefix)] == prefix and Journal.verify_chain(entries)
        assert sum(e["kind"] == "provider_recovery_allowance" for e in entries) == 1
        assert sum(e["kind"] == "provider_dispatch_intent" and e["payload"]["attempt"] == 5 for e in entries) == 1
        assert len(env.backend_calls) == 14
        assert first == await env.allowances.reserve(run_id, body)


async def test_native_competing_allowances_reserve_one_even_when_final_dispatch_fails(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        env = await failed_fourth(store, tmp_path, monkeypatch)
        run_id = env.run["id"]
        candidate = await env.allowances.candidate(run_id, "codex", adopt_current_executable=True)
        bodies = [allowance_request(candidate), allowance_request(candidate)]
        results = await asyncio.wait_for(
            asyncio.gather(*(env.allowances.reserve(run_id, b) for b in bodies), return_exceptions=True), 10
        )
        assert sum(isinstance(value, dict) for value in results) == 1
        assert sum(isinstance(value, ValueError) for value in results) == 1
        before = copy.deepcopy(await store.get_run(run_id))
        from firefly_forge import providers

        failed_provider = env.engine.provider_factory("codex")
        env.engine.configured_provider = True
        monkeypatch.setattr(providers, "get_provider", lambda agent, settings: failed_provider)
        await env.engine.execute_run(run_id)
        assert (await store.get_run(run_id))["status"] == "failed"
        with pytest.raises(ValueError):
            await env.allowances.candidate(run_id, "codex", adopt_current_executable=True)
        with pytest.raises(ValueError):
            await env.recovery.candidate(run_id, "codex")
        entries = await env.journal.entries(run_id)
        assert sum(e["kind"] == "provider_recovery_allowance" for e in entries) == 1
        assert sum(e["kind"] == "provider_dispatch_intent" and e["payload"]["attempt"] == 5 for e in entries) == 1
        assert (await store.get_run(run_id))["repair_budget"] == before["repair_budget"] == 0


@pytest.mark.parametrize("mutation", ["source", "tail", "revocation"])
async def test_native_stale_or_revoked_allowance_rolls_back_without_new_intent(store, tmp_path, monkeypatch, mutation):
    async with authenticated_operator(store) as actor:
        env = await failed_fourth(store, tmp_path, monkeypatch)
        run_id = env.run["id"]
        body = allowance_request(await env.allowances.candidate(run_id, "codex", adopt_current_executable=True))
        if mutation == "source":
            (env.engine.workspaces / run_id / "partial.py").write_text("CHANGED = True\n")
        elif mutation == "tail":
            async with store._unit():
                await env.journal._append(run_id, "implement", "diagnostic", "note", {})
        else:
            async with store._unit():
                user = await ForgeIdentityUserRepository().find_by_id(UUID(str(actor.id)))
                user.enabled = False
                await ForgeIdentityUserRepository().save(user)
        async with store._unit():
            prefix = await env.recovery._entries(run_id)
        with pytest.raises((ValueError, AccessDenied)):
            await env.allowances.reserve(run_id, body)
        async with store._unit():
            assert await env.recovery._entries(run_id) == prefix
        assert not any(e["kind"] == "provider_recovery_allowance" for e in prefix)
        assert (await store.get_run(run_id))["status"] == "failed"
        async with store._unit():
            assert (await env.journal._task(run_id, "implement")).attempt == 4


def test_native_allowance_http_di_strict_contract_and_exact_readback(tmp_path, postgres_schema):
    from starlette.testclient import TestClient
    from test_execution import seed_repository

    from firefly_forge.api import create_application
    from firefly_forge.engine import Engine
    from firefly_forge.git_workspace import metadata_digest
    from firefly_forge.models import Command, RepositoryInput, RunInput
    from firefly_forge.provider_recoveries import ProviderRecoveries

    token = "owned-final-allowance-api-fixture"
    with TestClient(
        create_application(tmp_path / "api", token, role="api"),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + token, "Origin": "http://testserver"},
        client=("127.0.0.1", 52551),
    ) as http:
        assert http.post("/api/v1/auth/login", json={"token": token}).status_code == 200
        store = http.app.state.store
        context = http.app.state.runtime.context
        engine = context.get_bean(Engine)
        recoveries = context.get_bean(ProviderRecoveries)
        assert context.get_bean(ProviderRecoveryAllowances).recoveries is recoveries
        assert engine.provider_recoveries is recoveries
        executable = tmp_path / "reviewed-api-cli"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        store.configuration.update("codex", ProviderConfiguration(values={"executable": str(executable)}))

        async def seed():
            async with authenticated_operator(store):
                repository = await store.add_repository(
                    RepositoryInput(
                        name="Final allowance controlled API fixture",
                        url=str(await seed_repository(tmp_path / "source")),
                    )
                )
                run = await store.create_run(
                    RunInput(
                        repository_id=repository["id"],
                        brief="Review an existing fourth failed native process without running a model in HTTP",
                        frontend="none",
                        skills=[],
                        commands=[Command(argv=["approved-check"])],
                    )
                )
                journal = context.get_bean(Journal)
                await journal.ensure_plan(run["id"])
                engine.workspaces.mkdir(parents=True, exist_ok=True)
                workspace = await seed_repository(engine.workspaces / run["id"])
                (workspace / "partial.py").write_text("VALUE = 1\n")
                await store.update(run["id"], git_metadata_digest=metadata_digest(workspace), active_task="implement")
                for attempt in range(1, 5):
                    intent = await journal.intent(run["id"], "implement", attempt)
                    await store.update(run["id"], status="developing", attempt=attempt)
                    await recoveries.normal_dispatch(run["id"], intent["idempotency_key"])
                    await recoveries.record_process(run["id"], intent["idempotency_key"], OUTCOME)
                await store.update(run["id"], status="failed", error="Controlled native process failure")
                return run["id"]

        run_id = http.portal.call(seed)
        endpoint = f"/api/v1/runs/{run_id}/provider-recoveries/allowances"
        assert http.get(endpoint + "/candidate?agent=codex").status_code == 409
        candidate = http.get(endpoint + "/candidate?agent=codex&adopt_current_executable=true")
        assert candidate.status_code == 200, candidate.text
        body = allowance_request(candidate.json()).model_dump()
        response = http.post(endpoint, json=body)
        assert response.status_code == 201, response.text
        assert http.post(endpoint, json=body).json() == response.json()
        assert http.get(endpoint + "/" + body["request_id"]).json() == response.json()
        assert http.post(endpoint, json=body | {"request_id": uuid4().hex}).status_code == 409
        assert http.post(endpoint, json=body | {"adopt_current_executable": False}).status_code == 422
        assert http.post(endpoint, json=body | {"legacy_attestation": {}}).status_code == 422
        legacy = http.get(f"/api/v1/runs/{run_id}/provider-recoveries/" + body["request_id"])
        assert legacy.status_code == 409 and "/allowances/" in legacy.json()["detail"]


@pytest.mark.parametrize("mutation", ["source", "binary", "revocation"])
async def test_native_reserved_allowance_drift_never_executes_provider(store, tmp_path, monkeypatch, mutation):
    from firefly_forge import providers

    async with authenticated_operator(store) as actor:
        env = await failed_fourth(store, tmp_path, monkeypatch)
        run_id = env.run["id"]
        body = allowance_request(await env.allowances.candidate(run_id, "codex", adopt_current_executable=True))
        await env.allowances.reserve(run_id, body)
        if mutation == "source":
            (env.engine.workspaces / run_id / "partial.py").write_text("CHANGED = True\n")
        elif mutation == "binary":
            (tmp_path / "reviewed-fixture-cli").write_text("#!/bin/sh\nexit 7\n")
        else:
            async with store._unit():
                user = await ForgeIdentityUserRepository().find_by_id(actor.id)
                user.enabled = False
                await ForgeIdentityUserRepository().save(user)
        calls = []

        class NeverProvider:
            async def execute(self, *args):
                calls.append(True)
                raise AssertionError("Drift must prevent actual provider execution")

        env.engine.configured_provider = True
        monkeypatch.setattr(providers, "get_provider", lambda agent, settings: NeverProvider())
        await env.engine.execute_run(run_id)
        assert calls == [] and len(env.backend_calls) == 7
        async with store._unit():
            entries = await env.recovery._entries(run_id)
        assert sum(e["kind"] == "provider_recovery_allowance" for e in entries) == 1
        assert not any(e["kind"] == "provider_dispatch_intent" and e["payload"]["attempt"] == 5 for e in entries)
        with pytest.raises((ValueError, AccessDenied)):
            await env.allowances.reserve(run_id, body.model_copy(update={"request_id": uuid4().hex}))


async def test_native_unknown_fourth_outcome_cannot_use_final_allowance(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        env = await failed_fourth(store, tmp_path, monkeypatch, unknown_fourth=True)
        run_id = env.run["id"]
        with pytest.raises(ValueError):
            await env.allowances.candidate(run_id, "codex", adopt_current_executable=True)
        entries = await env.journal.entries(run_id)
        assert not any(e["kind"] == "provider_recovery_allowance" for e in entries)
        assert (await store.get_run(run_id))["status"] == "needs_reconciliation"
