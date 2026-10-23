# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual PostgreSQL, Git, Journal and native API; coding and check executables are fixtures."""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import make_url
from test_execution import seed_repository
from worker_reply_fixtures import authenticated_operator

from firefly_forge.engine import Engine
from firefly_forge.git_workspace import metadata_digest
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.models import Command, RepositoryInput, RunInput
from firefly_forge.process import ProcessResult
from firefly_forge.project_access import AccessDenied
from firefly_forge.provider_recoveries import ProviderRecoveries
from firefly_forge.providers import AgentResult
from firefly_forge.sandbox import SandboxExecutor
from firefly_forge.store import Store
from firefly_forge.verification_handoffs import VerificationHandoffs
from firefly_forge.workspace_reconciliation_models import SOURCE_REFUSAL, WorkspaceReconciliationInput
from firefly_forge.workspace_reconciliations import WorkspaceReconciliations


async def retained_environment_refusal(store, tmp_path, monkeypatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FORGE_REPAIR_ATTEMPTS", "2")
    source = await seed_repository(tmp_path / "source")
    repo = await store.add_repository(RepositoryInput(name="Public environment fixture", url=str(source)))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            brief="Continue only the existing bounded native plan",
            frontend="none",
            skills=[],
            sandbox={"mode": "host", "command_limit": 35},
            commands=[Command(argv=["approved-check", str(i)], timeout=10) for i in range(7)],
        )
    )
    journal, calls, checks = Journal(store), [], []

    class Worker:
        async def execute(self, workspace, brief, *args):
            calls.append(brief)
            (workspace / "frontend").mkdir(exist_ok=True)
            (workspace / "partial.py").write_text(f"VALUE = {len(calls)}\n")
            if len(calls) > 1:
                return AgentResult("Owned fixture implementation is ready for native final checks", "fixture")
            return AgentResult(
                "Needs configured checks",
                "fixture",
                usage={
                    "worker_feedback": {
                        "id": "fixture-feedback",
                        "role_id": "implementer",
                        "task_id": "implement",
                        "attempt": 1,
                        "disposition": "blocked",
                        "summary": "Need approved verification evidence",
                        "request": {
                            "id": "fixture-request",
                            "kind": "verification",
                            "question": "Run configured checks?",
                            "context": "No new authority requested",
                        },
                    }
                },
            )

    class Backend:
        async def run(self, config, argv, cwd, timeout, limit, **kwargs):
            checks.append(list(argv))
            return ProcessResult(argv, 0, "Known owned check fixture")

    engine = Engine(store, tmp_path / "workspaces", lambda _: Worker(), journal=journal)
    engine.sandbox = SandboxExecutor(store, journal, backends=SimpleNamespace(select=lambda config: Backend()))
    handoffs = VerificationHandoffs(store, journal, engine)
    await store.acquire_ownership()
    await engine.execute_run(run["id"])
    assert (await store.get_run(run["id"]))["status"] == "paused"
    await handoffs.execute(run["id"])
    assert (await store.get_run(run["id"]))["status"] == "queued" and len(checks) == 7
    workspace = engine.workspaces / run["id"]
    # A disposable fixture recreates the old factory policy, not an application repair.
    (workspace / ".git/info/exclude").write_text("node_modules/\n.venv/\nvendor/\n")
    assert (workspace / "frontend").is_dir()
    dependencies = tmp_path / "public-dependencies"
    dependencies.mkdir()
    (dependencies / "marker").write_text("Public retained dependency fixture\n")
    (workspace / "frontend/node_modules").symlink_to(dependencies, target_is_directory=True)
    await store.update(run["id"], git_metadata_digest=metadata_digest(workspace))
    await engine.execute_run(run["id"])
    failed = await store.get_run(run["id"])
    assert failed["status"] == "failed" and failed["error"] == SOURCE_REFUSAL and len(calls) == 1
    assert next(t for t in (await journal.plan(run["id"]))["tasks"] if t["id"] == "implement")["attempt"] == 2
    recovery = ProviderRecoveries(store, journal, engine)
    return SimpleNamespace(
        store=store,
        run_id=run["id"],
        engine=engine,
        journal=journal,
        service=WorkspaceReconciliations(recovery),
        workspace=workspace,
        calls=calls,
        checks=checks,
    )


def admission(candidate):
    return WorkspaceReconciliationInput(
        request_id=uuid4().hex,
        candidate_digest=candidate["digest"],
        acknowledge_environment_only=True,
        acknowledge_resume_existing_task=True,
    )


async def committed_intents(schema, run_id, request_id):
    """Independent connection cannot see the caller transaction's uncommitted WAL."""
    import asyncpg

    url = make_url(os.environ["FORGE_TEST_DATABASE_URL"])
    connection = await asyncpg.connect(
        host=url.host, port=url.port, user=url.username, password=url.password, database=url.database, timeout=3
    )
    try:
        return await connection.fetchval(
            f'SELECT count(*) FROM "{schema}".forge_journal WHERE run_id=$1 AND idempotency_key=$2 '
            "AND kind='workspace_reconciliation_intent'",
            UUID(run_id),
            request_id,
        )
    finally:
        await connection.close()


async def test_native_committed_intent_precedes_effect_and_same_task_finishes_without_reset(
    store, tmp_path, monkeypatch
):
    async with authenticated_operator(store):
        env = await retained_environment_refusal(store, tmp_path, monkeypatch)
        from firefly_forge import workspace_reconciliations as module

        old, old_wal = await store.get_run(env.run_id), await env.journal.entries(env.run_id)
        candidate = await env.service.candidate(env.run_id)
        assert candidate["budget"] == {
            "attempt": 2,
            "implementation_limit": 4,
            "remaining_commands": 28,
            "final_checks": 7,
        }
        body, effects = admission(candidate), []
        apply = module.apply_dependency_plan

        def observed(workspace, plan):
            with ThreadPoolExecutor(max_workers=1) as pool:
                visible = pool.submit(
                    lambda: asyncio.run(committed_intents(store.schema, env.run_id, body.request_id))
                ).result(5)
            assert visible == 1
            effects.append(True)
            return apply(workspace, plan)

        monkeypatch.setattr(module, "apply_dependency_plan", observed)
        receipt = await env.service.admit(env.run_id, body)
        assert (
            receipt == await env.service.get(env.run_id, body.request_id) == await env.service.admit(env.run_id, body)
        )
        assert len(effects) == 1 and len(env.calls) == 1 and len(env.checks) == 7
        queued = await store.get_run(env.run_id)
        assert receipt["candidate"]["previous_run"] == {k: v for k, v in old.items() if k != "events"}
        for field in (
            "attempt",
            "implementation_rounds",
            "repair_budget",
            "sandbox_snapshot",
            "commands",
            "verification_handoff",
            "config_revision",
        ):
            assert queued[field] == old[field]
        assert "retry_count" not in queued
        await env.engine.execute_run(env.run_id)
        final = await store.get_run(env.run_id)
        (tmp_path / "native-final-run-and-WAL.json").write_text(
            json.dumps(
                {
                    "run": final,
                    "entries": await env.journal.entries(env.run_id),
                },
                indent=2,
            )
        )
        assert final["status"] == "succeeded", final.get("error")
        assert len(env.calls) == 2 and len(env.checks) == 14 and final["repair_budget"] == 2
        assert final["attempt"] == 2 and final["implementation_rounds"] == 2
        wal = await env.journal.entries(env.run_id)
        assert wal[: len(old_wal)] == old_wal and Journal.verify_chain(wal)


async def test_native_stale_handoff_source_refuses_review_before_any_environment_intent(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        env = await retained_environment_refusal(store, tmp_path, monkeypatch)
        (env.workspace / "partial.py").write_text("VALUE = 99\n")
        before = (env.workspace / ".git/index").read_bytes()
        with pytest.raises(ValueError, match="Automatic verification source changed"):
            await env.service.candidate(env.run_id)
        assert (env.workspace / ".git/index").read_bytes() == before
        assert not any(e["kind"].startswith("workspace_reconciliation") for e in await env.journal.entries(env.run_id))


@pytest.mark.parametrize("failure", ["cancel_after_effect", "sql_failure"])
async def test_native_partial_completion_stays_unknown_and_never_replays(store, tmp_path, monkeypatch, failure):
    async with authenticated_operator(store):
        env = await retained_environment_refusal(store, tmp_path, monkeypatch)
        body = admission(await env.service.candidate(env.run_id))
        if failure == "cancel_after_effect":
            from firefly_forge import workspace_reconciliations as module
            from firefly_forge.source_patch_admission import SourcePatch

            original = store.admit_credentials
            apply = module.apply_dependency_plan
            effects, admitted = [], []

            def completed_effect(*args):
                effect = apply(*args)
                effects.append(effect)
                return effect

            async def interrupted(patch):
                assert type(patch) is SourcePatch
                if effects:
                    assert patch.text == admitted[0]
                    raise asyncio.CancelledError()
                await original(patch.text)
                admitted.append(patch.text)

            monkeypatch.setattr(module, "apply_dependency_plan", completed_effect)
            monkeypatch.setattr(store, "vault_source_patch_admission", interrupted)
            error = asyncio.CancelledError
        else:

            async def rollback(*args):
                raise RuntimeError("Owned fixture injects a final SQL-unit rollback")

            monkeypatch.setattr(env.journal, "queue_environment_reconciled", rollback)
            error = RuntimeError
        with pytest.raises(error):
            await env.service.admit(env.run_id, body)
        if failure == "cancel_after_effect":
            assert len(effects) == len(admitted) == 1
        assert (await store.get_run(env.run_id))["status"] == "failed"
        assert (await env.service.get(env.run_id, body.request_id))["status"] == "needs_reconciliation"
        with pytest.raises(ValueError, match="unknown|reconciliation"):
            await env.service.admit(env.run_id, body)
        wal = await env.journal.entries(env.run_id)
        assert sum(e["kind"] == "workspace_reconciliation_intent" for e in wal) == 1
        assert not any(
            e["kind"] in {"workspace_reconciliation_receipt", "workspace_reconciliation_queued"} for e in wal
        )
        assert len(env.calls) == 1 and len(env.checks) == 7


async def test_native_identity_revocation_during_credential_wait_prevents_intent_and_effect(
    store, tmp_path, monkeypatch
):
    async with authenticated_operator(store) as actor:
        env = await retained_environment_refusal(store, tmp_path, monkeypatch)
        body = admission(await env.service.candidate(env.run_id))
        before = (env.workspace / ".git/index").read_bytes()
        original = store.admit_credentials

        async def revoked(value):
            await original(value)
            async with store._unit():
                user = await ForgeIdentityUserRepository().find_by_id(actor.id)
                user.enabled = False
                await ForgeIdentityUserRepository().save(user)

        monkeypatch.setattr(store, "admit_credentials", revoked)
        with pytest.raises(AccessDenied):
            await env.service.admit(env.run_id, body)
        assert (env.workspace / ".git/index").read_bytes() == before
        async with store._unit():
            entries = await env.journal.records.find_for_run(UUID(env.run_id))
            assert not any(e.kind.startswith("workspace_reconciliation") for e in entries)


def test_native_registered_api_requires_owner_and_returns_exact_original_receipt(
    tmp_path, postgres_schema, monkeypatch
):
    from starlette.testclient import TestClient

    from firefly_forge.api import create_application

    token = "owned-environment-api-fixture"
    with TestClient(
        create_application(tmp_path / "api", token, role="api"),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + token, "Origin": "http://testserver"},
        client=("127.0.0.1", 52550),
    ) as http:
        assert http.post("/api/v1/auth/login", json={"token": token}).status_code == 200
        store = http.app.state.store
        service = http.app.state.runtime.context.get_bean(WorkspaceReconciliations)
        engine = http.app.state.runtime.context.get_bean(Engine)
        assert service.recoveries.engine is engine and service.recoveries.store is store

        async def seed():
            fixture_store = Store(tmp_path / "seed-native" / "factory.db")
            await fixture_store.initialize()
            fixture_store.configuration = store.configuration
            try:
                async with authenticated_operator(fixture_store):
                    env = await retained_environment_refusal(fixture_store, tmp_path / "seed", monkeypatch)
                    engine.workspaces = env.engine.workspaces
                    run_id = env.run_id
            finally:
                await fixture_store.close()
            await store.acquire_ownership()
            return run_id

        run_id = http.portal.call(seed)
        endpoint = f"/api/v1/runs/{run_id}/workspace-reconciliations"
        response = http.get(endpoint + "/candidate")
        assert response.status_code == 200, response.text
        body = admission(response.json()).model_dump()
        receipt = http.post(endpoint, json=body)
        assert receipt.status_code == 201, receipt.text
        assert http.get(endpoint + "/" + body["request_id"]).json() == receipt.json()
        assert http.post(endpoint, json=body).json() == receipt.json()
        assert http.post(endpoint, json=body | {"attempt": 3}).status_code == 422
        http.portal.call(store.release_ownership)
        assert http.get(endpoint + "/candidate").status_code >= 400
