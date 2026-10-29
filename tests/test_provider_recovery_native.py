# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real PostgreSQL, journal, Git and Engine; external inference and command execution are fixtures."""

import asyncio
from uuid import UUID, uuid4

import pytest
from test_automatic_verification_native import prepared
from test_execution import seed_repository
from worker_reply_fixtures import authenticated_operator

from firefly_forge.configuration import ProviderConfiguration
from firefly_forge.engine import Engine
from firefly_forge.git_workspace import metadata_digest
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.models import Command, RepositoryInput, RunInput
from firefly_forge.provider_recoveries import ProviderRecoveries
from firefly_forge.provider_recovery_models import ProviderRecoveryAdmission
from firefly_forge.providers import AgentResult, ProviderError


async def legacy_failure(store, tmp_path, monkeypatch):
    env = await prepared(store, tmp_path, monkeypatch)
    await env.service.execute(env.run["id"])

    class FailedProvider:
        async def execute(self, workspace, *args):
            (workspace / "retained-note.txt").write_text("Partial source survives the failed process\n")
            raise ProviderError("Official CLI exited with signal 25; historical fixture")

    env.engine.provider_factory = lambda _: FailedProvider()
    await env.engine.execute_run(env.run["id"])
    current = await store.get_run(env.run["id"])
    assert current["status"] == "failed", current.get("error")
    assert current["implementation_rounds"] == 1 and current["attempt"] == 2
    assert current["repair_budget"] == 0
    assert len(env.backend_calls) == 7
    assert (env.engine.workspaces / env.run["id"] / "retained-note.txt").exists()
    env.recovery = ProviderRecoveries(store, env.journal, env.engine)
    env.engine.provider_recoveries = env.recovery
    return env


def admission(candidate):
    return ProviderRecoveryAdmission(
        request_id=uuid4().hex,
        candidate_digest=candidate["digest"],
        agent="codex",
        legacy_attestation={
            "provenance": "legacy_operator_attestation",
            "evidence_digest": "a" * 64,
            "trusted_operator_observation": True,
            "process_terminated": True,
            "process_group_drained": True,
            "no_accepted_final_result": True,
            "exit_code": -25,
        },
    )


async def test_native_legacy_recovery_preserves_spent_verification_and_untracked_source(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        env = await legacy_failure(store, tmp_path, monkeypatch)
        run_id = env.run["id"]
        before = await store.get_run(run_id)
        old_entries = await env.journal.entries(run_id)
        candidate = await env.recovery.candidate(run_id, "codex")
        assert candidate["next_attempt"] == 3 and candidate["dispatches_used"] == 2
        assert candidate["process_evidence"] == "legacy_operator_attestation_required"
        body = admission(candidate)
        first, repeated = await asyncio.wait_for(
            asyncio.gather(env.recovery.admit(run_id, body), env.recovery.admit(run_id, body)), 10
        )
        assert first == repeated == await env.recovery.get(run_id, body.request_id)
        assert first["process_evidence"]["provenance"] == "legacy_operator_attestation"
        assert first["previous_task"]["status"] == "running"
        queued = await store.get_run(run_id)
        for key in (
            "implementation_rounds",
            "repair_budget",
            "verification_handoff",
            "worker_response",
            "config_revision",
        ):
            assert queued[key] == before[key]
        calls = []

        class RecoveredProvider:
            async def execute(self, workspace, *args):
                calls.append(workspace)
                assert (workspace / "retained-note.txt").read_text() == "Partial source survives the failed process\n"
                assert (workspace / "partial.py").read_text() == "VALUE = 1\n"
                (workspace / "partial.py").write_text("VALUE = 2\n")
                return AgentResult("Completed the retained implementation", "fixture")

        env.engine.provider_factory = lambda _: RecoveredProvider()
        await env.engine.execute_run(run_id)
        final = await store.get_run(run_id)
        assert final["status"] == "succeeded", final.get("error")
        assert final["attempt"] == 3 and final["implementation_rounds"] == 2 and final["repair_budget"] == 0
        assert len(calls) == 1 and calls[0] == env.engine.workspaces / run_id
        assert len(env.backend_calls) == 14 and len(final["final_verification"]) == 7
        assert final["provider_recovery"]["status"] == "completed"
        entries = await env.journal.entries(run_id)
        assert entries[: len(old_entries)] == old_entries
        assert Journal.verify_chain(entries)
        for kind in ("provider_recovery_admission", "provider_failure_closed", "provider_workspace_prepared"):
            assert sum(entry["kind"] == kind for entry in entries) == 1
        assert sum(entry["kind"] == "verification_continuation_intent" for entry in entries) == 1
        assert sum(entry["kind"] == "provider_dispatch_intent" for entry in entries) == 1
        assert "retained-note.txt" in final["changed_files"] and "VALUE = 2" in final["diff"]
        assert await env.recovery.get(run_id, body.request_id) == first


async def test_native_changed_source_refuses_admission_and_reserved_dispatch(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        env = await legacy_failure(store, tmp_path, monkeypatch)
        run_id = env.run["id"]
        workspace = env.engine.workspaces / run_id
        body = admission(await env.recovery.candidate(run_id, "codex"))
        (workspace / "retained-note.txt").write_text("Changed after review\n")
        with pytest.raises(ValueError, match="candidate changed"):
            await env.recovery.admit(run_id, body)
        assert (await store.get_run(run_id))["status"] == "failed"
        body = admission(await env.recovery.candidate(run_id, "codex"))
        await env.recovery.admit(run_id, body)
        (workspace / "retained-note.txt").write_text("Changed after reservation\n")
        calls = []

        class NeverProvider:
            async def execute(self, *args):
                calls.append(True)
                raise AssertionError("Changed source must not dispatch a provider")

        env.engine.provider_factory = lambda _: NeverProvider()
        await env.engine.execute_run(run_id)
        assert calls == [] and len(env.backend_calls) == 7
        assert not any(row["kind"] == "provider_dispatch_intent" for row in await env.journal.entries(run_id))
        assert (await store.get_run(run_id))["status"] != "succeeded"


async def test_native_cli_adoption_is_one_dispatch_and_preserves_frozen_configuration(store, tmp_path, monkeypatch):
    from firefly_forge import providers

    old_cli, new_cli = tmp_path / "retained-cli", tmp_path / "updated-cli"
    for path in (old_cli, new_cli):
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    monkeypatch.setenv("FORGE_CODEX_EXECUTABLE", str(old_cli))
    async with authenticated_operator(store):
        env = await legacy_failure(store, tmp_path, monkeypatch)
        run_id = env.run["id"]
        original = await store.get_run(run_id)
        frozen = store.configuration.load_snapshot(original["config_revision"])
        store.configuration.update(
            "codex", ProviderConfiguration(values={"executable": str(new_cli), "model": "different-model"})
        )
        candidate = await env.recovery.candidate(run_id, "codex", adopt_current_executable=True)
        request = admission(candidate).model_copy(update={"adopt_current_executable": True})
        receipt = await env.recovery.admit(run_id, request)
        calls = []

        class Worker:
            def __init__(self, settings):
                self.settings = settings

            async def execute(self, workspace, brief, backend, frontend, emit):
                calls.append(dict(self.settings))
                result = await providers.run_process(
                    [self.settings["FORGE_CODEX_EXECUTABLE"]], workspace, emit, timeout=2
                )
                assert result.returncode == 0
                (workspace / "partial.py").write_text(f"VALUE = {len(calls) + 1}\n")
                return AgentResult("Completed controlled native fixture", "fixture")

        def select(agent, settings=None):
            assert agent == "codex"
            return Worker(settings)

        monkeypatch.setattr(providers, "get_provider", select)
        env.engine.configured_provider = True
        await env.engine.execute_run(run_id)
        final = await store.get_run(run_id)
        assert final["status"] == "succeeded", final.get("error")
        assert calls == [{**frozen, "FORGE_CODEX_EXECUTABLE": str(new_cli)}]
        assert env.recovery.execution_settings(final, frozen, final["provider_recovery"]["task_key"]) == frozen
        assert frozen["FORGE_CODEX_EXECUTABLE"] == str(old_cli)
        assert final["config_revision"] == original["config_revision"]
        assert final["verification_handoff"] == original["verification_handoff"]
        assert final["repair_budget"] == original["repair_budget"] == 0
        assert store.configuration.load_snapshot(final["config_revision"]) == frozen
        assert receipt == await env.recovery.get(run_id, request.request_id)
        entries = await env.journal.entries(run_id)
        assert Journal.verify_chain(entries)
        dispatches = [row for row in entries if row["kind"] == "provider_dispatch_intent"]
        assert len(dispatches) == 1
        assert dispatches[0]["payload"]["executable_adoption"] == candidate["executable_adoption"]


def test_native_registered_recovery_api_exact_receipt_and_revoked_operator(tmp_path, postgres_schema):
    from native_client_knowledge_fixtures import activate_native_client_target
    from starlette.testclient import TestClient

    from firefly_forge.api import create_application

    token = "owned-failed-provider-api-fixture"
    with TestClient(
        create_application(tmp_path / "api", token, role="api"),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + token, "Origin": "http://testserver"},
        client=("127.0.0.1", 52550),
    ) as http:
        assert http.post("/api/v1/auth/login", json={"token": token}).status_code == 200
        store = http.app.state.store
        http.portal.call(activate_native_client_target, store, tmp_path / "framework-prerequisite")
        engine = http.app.state.runtime.context.get_bean(Engine)
        recoveries = http.app.state.runtime.context.get_bean(ProviderRecoveries)
        assert recoveries.engine is engine and engine.provider_recoveries is recoveries

        async def seed():
            async with authenticated_operator(store):
                source = await seed_repository(tmp_path / "source")
                repository = await store.add_repository(
                    RepositoryInput(name="Retained failure fixture", url=str(source))
                )
                run = await store.create_run(
                    RunInput(
                        repository_id=repository["id"],
                        brief="Inspect a historical failed CLI receipt without executing from the API role",
                        frontend="none",
                        skills=[],
                        commands=[Command(argv=["approved-check"])],
                    )
                )
                journal = http.app.state.runtime.context.get_bean(Journal)
                await journal.ensure_plan(run["id"])
                await journal.intent(run["id"], "implement")
                engine.workspaces.mkdir(parents=True, exist_ok=True)
                workspace = await seed_repository(engine.workspaces / run["id"])
                (workspace / "partial.py").write_text("VALUE = 1\n")
                await store.update(
                    run["id"],
                    status="failed",
                    active_task="implement",
                    attempt=1,
                    implementation_rounds=0,
                    repair_budget=0,
                    error="Historical CLI failure fixture",
                    git_metadata_digest=metadata_digest(workspace),
                )
                return run["id"]

        run_id = http.portal.call(seed)
        endpoint = f"/api/v1/runs/{run_id}/provider-recoveries"
        candidate = http.get(endpoint + "/candidate?agent=codex")
        assert candidate.status_code == 200, candidate.text
        assert "executable_adoption" not in candidate.json()
        executable = tmp_path / "configured-cli"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        store.configuration.update("codex", ProviderConfiguration(values={"executable": str(executable)}))
        candidate = http.get(endpoint + "/candidate?agent=codex&adopt_current_executable=true")
        assert candidate.status_code == 200, candidate.text
        assert candidate.json()["executable_adoption"]["path"] == str(executable)
        body = admission(candidate.json()).model_dump() | {"adopt_current_executable": True}
        response = http.post(endpoint, json=body)
        assert response.status_code == 201, response.text
        assert http.post(endpoint, json=body).json() == response.json()
        exact = endpoint + "/" + body["request_id"]
        assert http.get(exact).json() == response.json()
        assert http.post(endpoint, json={**body, "candidate_digest": "f" * 64}).status_code == 409
        assert http.post(endpoint, json={**body, "native_process_digest": "b" * 64}).status_code == 422

        async def disable():
            async with store._unit():
                user = await ForgeIdentityUserRepository().find_by_id(UUID("eb6e9e6f-4311-5435-a84f-e35ae14c19af"))
                user.enabled = False
                await ForgeIdentityUserRepository().save(user)

        http.portal.call(disable)
        assert http.get(exact).status_code == 401
