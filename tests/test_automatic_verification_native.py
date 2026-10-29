# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native PostgreSQL ledger qualification; only the external command backend is substituted."""

import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

from test_execution import seed_repository
from worker_reply_fixtures import authenticated_operator

from firefly_forge.engine import Engine
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.models import Command, RepositoryInput, RunInput
from firefly_forge.persistence import ForgeRunEntity
from firefly_forge.process import ProcessResult
from firefly_forge.providers import AgentResult
from firefly_forge.sandbox import SandboxExecutor
from firefly_forge.verification_handoffs import VerificationHandoffs, automatic_candidate, verification_intent


async def prepared(store, tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_REPAIR_ATTEMPTS", "0")
    source = await seed_repository(tmp_path / "source")
    repo = await store.add_repository(RepositoryInput(name="Verification fixture", url=str(source)))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            brief="Preserve source through bounded verification",
            frontend="none",
            skills=[],
            commands=[Command(argv=["approved-check", str(index)], timeout=10) for index in range(7)],
        )
    )
    journal = Journal(store)
    calls = []

    class Worker:
        async def execute(self, workspace, brief, *args):
            calls.append(brief)
            (workspace / "partial.py").write_text("VALUE = 1\n")
            return AgentResult(
                "Needs approved verification evidence",
                "fixture",
                usage={
                    "worker_feedback": {
                        "id": f"feedback-{len(calls)}",
                        "role_id": "implementer",
                        "task_id": "implement",
                        "attempt": len(calls),
                        "disposition": "blocked",
                        "summary": "Configured command evidence is needed",
                        "request": {
                            "id": f"arbitrary-{len(calls)}",
                            "kind": "verification",
                            "question": "Can the configured checks run?",
                            "context": "No new commands or authority requested",
                        },
                    }
                },
            )

    backend_calls = []

    class Backend:
        async def run(self, config, argv, cwd, timeout, limit, **kwargs):
            backend_calls.append(list(argv))
            return ProcessResult(argv, 0, "Known external backend fixture receipt")

    backend = Backend()
    engine = Engine(store, tmp_path / "workspaces", lambda _: Worker(), journal=journal)
    engine.sandbox = SandboxExecutor(store, journal, backends=SimpleNamespace(select=lambda config: backend))
    service = VerificationHandoffs(store, journal, engine)
    await store.acquire_ownership()
    await engine.execute_run(run["id"])
    assert (await store.get_run(run["id"]))["status"] == "paused"
    return SimpleNamespace(
        run=run,
        repo=repo,
        journal=journal,
        engine=engine,
        service=service,
        calls=calls,
        backend=backend,
        backend_calls=backend_calls,
    )


async def test_native_repair_zero_seven_receipts_one_continuation_then_hold(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        env = await prepared(store, tmp_path, monkeypatch)
        await env.service.execute(env.run["id"])
        assert (await store.get_run(env.run["id"]))["status"] == "queued"
        assert len(env.backend_calls) == 7
        assert len(await env.journal.sandbox_executions(env.run["id"])) == 7
        await env.engine.execute_run(env.run["id"])
        current = await store.get_run(env.run["id"])
        assert len(env.calls) == 2 and current["implementation_rounds"] == 2 and current["repair_budget"] == 0
        await env.service.execute(env.run["id"])
        current = await store.get_run(env.run["id"])
        assert current["worker_request"]["status"] == "pending" and current["verification_refusal"]["status"] == "held"
        assert not automatic_candidate(current) and len(env.backend_calls) == 7
        assert Journal.verify_chain(await env.journal.entries(env.run["id"]))


async def test_native_known_receipt_recovery_preserves_attempt_without_reexecution(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        env = await prepared(store, tmp_path, monkeypatch)
        current = await store.get_run(env.run["id"])
        workspace = env.engine.workspaces / env.run["id"]
        intent = verification_intent(store, current, workspace, await env.journal.entries(env.run["id"]), [])
        from firefly_forge.verification_models import check_verification_cut

        await env.journal.verification_begin(
            env.run["id"], intent, check_current=lambda run: check_verification_cut(store, run, intent)
        )
        command = current["commands"][0]
        await env.engine.sandbox.execute(
            env.run["id"],
            workspace,
            workspace,
            command["argv"],
            command["timeout"],
            "implement",
            ordinal=intent["ordinals"][0],
            git_root=workspace,
        )
        await env.journal.recover()
        assert (
            next(task for task in (await env.journal.plan(env.run["id"]))["tasks"] if task["id"] == "implement")[
                "status"
            ]
            == "needs_reconciliation"
        )
        await env.service.execute(env.run["id"])
        assert (await store.get_run(env.run["id"]))["status"] == "queued"
        assert len(env.backend_calls) == 7
        executions = await env.journal.sandbox_executions(env.run["id"])
        assert all(row["parent_attempt"] == 1 for row in executions)


async def test_native_unknown_effect_and_revoked_identity_never_continue(store, tmp_path, monkeypatch):
    async with authenticated_operator(store) as actor:
        env = await prepared(store, tmp_path, monkeypatch)

        async def uncertain(config, argv, *args, **kwargs):
            env.backend_calls.append(argv)
            async with store._unit():
                user = await ForgeIdentityUserRepository().find_by_id(actor.id)
                user.enabled = False
                await ForgeIdentityUserRepository().save(user)
            raise TimeoutError("Unknown external command outcome")

        env.backend.run = uncertain
        await env.service.execute(env.run["id"])
        current = await store.get_run(env.run["id"])
        assert current["verification_handoff"]["status"] == "held"
        assert current["worker_request"]["status"] == "pending"
        await env.service.execute(env.run["id"])
        assert len(env.calls) == 1 and len(env.backend_calls) == 1
        assert not automatic_candidate(await store.get_run(env.run["id"]))


async def test_native_manual_and_held_rows_do_not_starve_later_eligible_work(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        env = await prepared(store, tmp_path, monkeypatch)
        async with store._unit():
            # Earlier sequence values are explicit fixture rows, never admitted execution work.
            eligible = await store.run_records.find_by_id(UUID(env.run["id"]))
            eligible.sequence = 1000
            await store.run_records.save(eligible)
            for index in range(100):
                key = uuid4()
                await store.run_records.save(
                    ForgeRunEntity(
                        id=key,
                        repository_id=UUID(env.repo["id"]),
                        status="paused",
                        data={
                            "id": key.hex,
                            "status": "paused",
                            "worker_request": {
                                "id": f"manual-{index}",
                                "kind": "verification",
                                "status": "pending",
                                "task_id": "implement",
                                "attempt": 1,
                            },
                            "tool_policy_snapshot": env.run["tool_policy_snapshot"] if index % 2 else None,
                            "verification_handoff": {"request_id": f"manual-{index}", "status": "held"}
                            if index % 2
                            else {},
                        },
                    )
                )
        assert env.run["id"] in await env.service.ready()


def test_native_registered_legacy_adoption_exact_readback_and_disabled_identity(tmp_path, postgres_schema):
    from native_client_knowledge_fixtures import activate_native_client_target
    from starlette.testclient import TestClient

    from firefly_forge.agent_roles import WorkerFeedback, WorkerRequest
    from firefly_forge.api import create_application

    token = "owned-verification-adoption-api-fixture"
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
        journal = http.app.state.runtime.context.get_bean(Journal)

        async def seed():
            repo = await store.add_repository(RepositoryInput(name="Legacy retained source", url=str(tmp_path)))
            run = await store.create_run(
                RunInput(
                    repository_id=repo["id"],
                    brief="Retained typed verification fixture",
                    frontend="none",
                    commands=[Command(argv=["approved-check"])],
                )
            )
            await store.update(run["id"], tool_policy_snapshot=None)
            await journal.ensure_plan(run["id"])
            await journal.intent(run["id"], "implement")
            await journal.worker_feedback(
                run["id"],
                WorkerFeedback(
                    id="legacy-feedback",
                    role_id="implementer",
                    task_id="implement",
                    attempt=1,
                    disposition="blocked",
                    summary="Needs configured evidence",
                    request=WorkerRequest(
                        id="arbitrary-legacy",
                        kind="verification",
                        question="Run approved checks?",
                        context="No arbitrary command admission",
                    ),
                ),
            )
            workspace = engine.workspaces / run["id"]
            workspace.mkdir()
            (workspace / "partial.py").write_text("VALUE = 1\n")
            return run

        run = http.portal.call(seed)
        endpoint = f"/api/v1/runs/{run['id']}/verification-handoffs"
        candidate = http.get(endpoint + "/candidates?agent=codex")
        assert candidate.status_code == 200, candidate.text
        data = candidate.json()
        body = {
            "request_id": uuid4().hex,
            "agent": "codex",
            **{
                key: data[key]
                for key in (
                    "pending_request_digest",
                    "source_digest",
                    "commands_digest",
                    "policy_digest",
                    "provider_digest",
                )
            },
        }
        first = http.post(endpoint + "/adoptions", json=body)
        assert first.status_code == 201, first.text
        assert http.post(endpoint + "/adoptions", json=body).json() == first.json()
        assert http.get(endpoint + "/adoptions/" + body["request_id"]).json() == first.json()
        assert http.post(endpoint + "/adoptions", json={**body, "source_digest": "f" * 64}).status_code == 409
        assert http.post(endpoint + "/adoptions", json={**body, "source_digest": None}).status_code == 422
        assert http.portal.call(store.get_run, run["id"])["status"] == "paused"

        async def disable():
            async with store._unit():
                user = await ForgeIdentityUserRepository().find_by_id(UUID("eb6e9e6f-4311-5435-a84f-e35ae14c19af"))
                user.enabled = False
                await ForgeIdentityUserRepository().save(user)

        http.portal.call(disable)
        assert http.get(endpoint + "/adoptions/" + body["request_id"]).status_code == 401


async def test_native_adoption_and_human_reply_share_lock_order_without_duplicate_effects(store, tmp_path, monkeypatch):
    from firefly_forge.agent_roles import WorkerResponse
    from firefly_forge.verification_models import VerificationAdoption

    async with authenticated_operator(store):
        env = await prepared(store, tmp_path, monkeypatch)
        await store.update(env.run["id"], tool_policy_snapshot=None)
        candidate = await env.service.candidates(env.run["id"], "codex")
        body = VerificationAdoption(
            request_id=uuid4().hex,
            agent="codex",
            **{
                key: candidate[key]
                for key in (
                    "pending_request_digest",
                    "source_digest",
                    "commands_digest",
                    "policy_digest",
                    "provider_digest",
                )
            },
        )
        results = await asyncio.wait_for(
            asyncio.gather(
                env.service.adopt(env.run["id"], body),
                env.journal.worker_response(
                    env.run["id"],
                    "arbitrary-1",
                    WorkerResponse(id="human-answer", actor="Native operator", answer="Keep this request manual"),
                ),
                return_exceptions=True,
            ),
            10,
        )
        assert not all(isinstance(result, Exception) for result in results)
        current = await store.get_run(env.run["id"])
        assert current["worker_request"]["status"] == "answered"
        assert len(env.backend_calls) == 0 and len(env.calls) == 1
        assert (
            len([row for row in await env.journal.worker_messages(env.run["id"]) if row["kind"] == "worker_response"])
            == 1
        )
