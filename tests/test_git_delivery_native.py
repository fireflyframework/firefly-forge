# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native owned custody and registered API use public local Git fixtures, never a model/account."""

import asyncio
import json
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pyfly.data.relational.sqlalchemy import LockMode
from sqlalchemy import text
from worker_reply_fixtures import authenticated_operator

from firefly_forge.agent_roles import WorkerResponse
from firefly_forge.engine import Engine
from firefly_forge.execution_requests import ExecutionRequests
from firefly_forge.git_deliveries import GitDeliveries
from firefly_forge.git_delivery_models import GitDeliveryPolicyInputV1
from firefly_forge.git_delivery_policy import GitDeliveryPolicies
from firefly_forge.git_delivery_requests import GitDeliveryRequests
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.providers import AgentResult
from firefly_forge.runtime_roles import RuntimeRoles
from firefly_forge.scm import checked
from firefly_forge.store import Store


async def source_project(store, root):
    source = root / "source"
    source.mkdir()
    (source / "product.py").write_text("value = 1\n")
    await checked(["git", "init", "--initial-branch=main"], source)
    await checked(["git", "add", "product.py"], source)
    await checked(
        [
            "git",
            "-c",
            "user.name=Public fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "Original fixture",
        ],
        source,
    )
    repository = await store.add_repository(RepositoryInput(name="Checkpoint public fixture", url=str(source)))
    project = await Projects(store).configure(
        ProjectDefinition(
            name="Checkpoint fixture",
            description="Public owned checkpoint fixture",
            topology="monorepo",
            owners=["Fixture owner"],
            components=[
                {
                    "id": "core",
                    "name": "Core",
                    "repository_id": repository["id"],
                    "path": ".",
                    "responsibility": "Own the public value",
                    "verification": [{"argv": [sys.executable, "-c", "from product import value; assert value == 3"]}],
                }
            ],
        )
    )
    return repository, project


async def paused(store, root, repository, project, *, on_pause, workspaces=None):
    await GitDeliveryPolicies(store).configure(
        project["id"],
        GitDeliveryPolicyInputV1(
            version=1,
            request_id=uuid4().hex,
            expected_previous_digest=None,
            policy={"enabled": True, "every_units": 1, "on_pause": on_pause, "remote": "local", "max_checkpoints": 50},
        ),
    )
    run = await store.create_run(
        RunInput(
            repository_id=repository["id"],
            project_id=project["id"],
            component_id="core",
            brief="Maintain the public value",
            skills=[],
        )
    )
    calls = []

    class Worker:
        async def execute(self, workspace, *args):
            calls.append(workspace)
            (workspace / "product.py").write_text("value = 2\n" if len(calls) == 1 else "value = 3\n")
            if len(calls) == 1:
                feedback = {
                    "id": "checkpoint-question-feedback",
                    "role_id": "implementer",
                    "task_id": "implement",
                    "attempt": 1,
                    "disposition": "clarification",
                    "summary": "A public value decision is pending",
                    "evidence_paths": [],
                    "request": {
                        "id": "checkpoint-value-question",
                        "kind": "other",
                        "question": "Use the next public value?",
                        "context": "The fixture awaits the current owner.",
                        "choices": ["Use 3", "Stop"],
                    },
                }
                return AgentResult(
                    json.dumps({"summary": "Public decision pending", "feedback": feedback}), "controlled fixture"
                )
            return AgentResult("Changed the public value after the recorded answer", "controlled fixture")

    journal = Journal(store)
    engine = Engine(store, workspaces or root / "workspaces", lambda _: Worker(), journal=journal)
    engine.enforce_ownership = True
    delivery = GitDeliveries(store, engine, journal)
    engine.git_deliveries = delivery
    engine.active[run["id"]] = asyncio.current_task()
    try:
        await engine.execute_run(run["id"])
    finally:
        engine.active.pop(run["id"], None)
    current = await store.get_run(run["id"])
    assert current["status"] == "paused", current.get("error")
    assert current["worker_request"]["status"] == "pending" and len(calls) == 1
    return SimpleNamespace(
        run=current,
        engine=engine,
        journal=journal,
        delivery=delivery,
        calls=calls,
        workspace=engine.workspaces / run["id"],
    )


async def test_native_paused_checkpoint_keeps_owned_session_and_resumes_from_factory_head(store, tmp_path, monkeypatch):
    async with authenticated_operator(store):
        await store.acquire_ownership()
        repository, project = await source_project(store, tmp_path)
        witnesses = []
        append = Journal.git_delivery_append

        async def observed(journal, run_id, kind, key, payload):
            async with journal.store._unit() as session:
                pid = await session.scalar(text("SELECT pg_backend_pid()"))
            assert pid == journal.store._ownership_pid
            witnesses.append((kind, pid))
            return await append(journal, run_id, kind, key, payload)

        monkeypatch.setattr(Journal, "git_delivery_append", observed)
        env = await paused(store, tmp_path, repository, project, on_pause=True)
        first = (await env.delivery.view(env.run["id"]))["items"]
        assert len(first) == 1 and first[0]["revision"] != env.run["base_revision"]
        assert await checked(["git", "rev-parse", "HEAD"], env.workspace) == first[0]["revision"]
        assert await checked(["git", "show", env.run["base_revision"] + ":product.py"], env.workspace) == "value = 1"
        current_policy = await GitDeliveryPolicies(store).view(project["id"])
        await GitDeliveryPolicies(store).configure(
            project["id"],
            GitDeliveryPolicyInputV1(
                version=1,
                request_id=uuid4().hex,
                expected_previous_digest=current_policy["digest"],
                policy={"enabled": True, "every_units": 2},
            ),
        )
        await env.journal.worker_response(
            env.run["id"],
            "checkpoint-value-question",
            WorkerResponse(id="checkpoint-owner-answer", actor="Authenticated owner", answer="Use 3"),
        )
        await env.journal.resume(env.run["id"])
        env.engine.active[env.run["id"]] = asyncio.current_task()
        try:
            await env.engine.execute_run(env.run["id"])
        finally:
            env.engine.active.pop(env.run["id"], None)
        final = await store.get_run(env.run["id"])
        assert final["status"] == "succeeded", final.get("error")
        assert final["base_revision"] == env.run["base_revision"] and final["git_delivery_policy"]["every_units"] == 1
        items = (await env.delivery.view(env.run["id"]))["items"]
        assert len(items) == 2 and items[1]["parent_revision"] == items[0]["revision"]
        assert final["checkpoint_revision"] == items[1]["revision"]
        assert await checked(["git", "rev-parse", "HEAD^{tree}"], env.workspace) == final["source_tree"]
        assert witnesses and all(pid == store._ownership_pid for _, pid in witnesses)
        assert env.journal.verify_chain(await env.journal.entries(env.run["id"]))


def test_native_api_queues_checkpoint_and_revalidates_actor_before_worker_effect(tmp_path, postgres_schema):
    from starlette.testclient import TestClient

    from firefly_forge.api import create_application

    token = "public-intermediate-git-native-api-fixture"
    with TestClient(
        create_application(tmp_path / "api", token, role="api"),
        base_url="http://testserver",
        headers={"Authorization": "Bearer " + token, "Origin": "http://testserver"},
        client=("127.0.0.1", 52990),
    ) as http:
        assert http.post("/api/v1/auth/login", json={"token": token}).status_code == 200
        api_store = http.app.state.store
        context = http.app.state.runtime.context
        assert context.get_bean(GitDeliveryPolicies).store is api_store
        assert context.get_bean(GitDeliveries).store is api_store
        assert context.get_bean(GitDeliveryRequests).store is api_store
        with pytest.raises(RuntimeError, match="API role"):
            api_store.runtime_roles.require_execution()

        async def seed():
            worker = Store(api_store.path)
            worker.configuration = api_store.configuration
            runtime = RuntimeRoles(worker, "worker")
            worker.runtime_roles = runtime
            try:
                await worker.initialize()
                await runtime.start()
                await worker.acquire_ownership()
                async with authenticated_operator(worker) as actor:
                    repository, project = await source_project(worker, tmp_path)
                    env = await paused(
                        worker,
                        tmp_path,
                        repository,
                        project,
                        on_pause=False,
                        workspaces=http.app.state.engine.workspaces,
                    )
                    env.actor = actor
                    env.store = worker
                    env.runtime = runtime
                    env.requests = ExecutionRequests(worker, runtime)
                    env.tickets = GitDeliveryRequests(env.delivery, env.requests)
                    baseline_tree = await checked(["git", "rev-parse", "HEAD^{tree}"], env.workspace)
                    assert await checked(["git", "write-tree"], env.workspace) == baseline_tree
                    files, _patch, tree = await env.engine._snapshot(
                        env.workspace, env.run["base_revision"], expected_head=env.run["base_revision"]
                    )
                    assert files == ["product.py"] and tree != baseline_tree
                    return env, tree
            except BaseException:
                try:
                    await runtime.stop()
                finally:
                    await worker.close()
                raise

        env, tree = http.portal.call(seed)
        run_id = env.run["id"]
        path = f"/api/v1/runs/{run_id}/git-deliveries"
        first_id = uuid4().hex
        body = {
            "version": 1,
            "request_id": first_id,
            "expected_head": env.run["base_revision"],
            "expected_source_tree": tree,
        }
        try:
            response = http.post(path, json=body)
            assert response.status_code == 202, response.text
            assert response.json()["status"] == "queued"
            assert http.portal.call(checked, ["git", "rev-parse", "HEAD"], env.workspace) == env.run["base_revision"]

            async def enabled(value):
                async with env.store._unit():
                    repository = ForgeIdentityUserRepository()
                    actor = await repository.find_by_id(env.actor.id, lock=LockMode.PESSIMISTIC_WRITE)
                    actor.enabled = value
                    await repository.save(actor)

            http.portal.call(enabled, False)
            http.portal.call(env.tickets.execute, first_id)
            denied = http.portal.call(env.requests.get, first_id)
            assert denied["status"] == "failed" and not any(
                e["kind"] == "git_delivery_intent" for e in http.portal.call(env.journal.entries, run_id)
            )
            http.portal.call(enabled, True)
            second_id = uuid4().hex
            response = http.post(path, json={**body, "request_id": second_id})
            assert response.status_code == 202, response.text
            http.portal.call(env.tickets.execute, second_id)
            receipt = http.get(path + "/requests/" + second_id)
            assert receipt.status_code == 200, receipt.text
            assert receipt.json()["status"] == "completed", receipt.text
            history = http.get(path)
            assert history.status_code == 200, history.text
            assert len(history.json()["items"]) == 1
            assert history.json()["items"][0]["revision"] == receipt.json()["result"]["revision"]
            assert env.run["base_revision"] == http.portal.call(env.store.get_run, run_id)["base_revision"]
        finally:
            http.portal.call(env.runtime.stop)
            http.portal.call(env.store.close)
