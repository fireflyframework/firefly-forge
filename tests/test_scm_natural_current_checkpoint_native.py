# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual native owner queue and local Git; only provider/SCM transports are controlled."""

import asyncio
import hashlib
from uuid import uuid4

import pytest
from scm_polling_native_fixtures import native_poll as native_poll
from test_git_delivery_native import paused
from test_scm_natural_native import observe, prepare
from worker_reply_fixtures import authenticated_operator

from firefly_forge import engine as engine_module
from firefly_forge.collaboration import CollaborationSettings
from firefly_forge.execution_requests import ExecutionRequests
from firefly_forge.git_delivery_requests import GitDeliveryRequests
from firefly_forge.project_access import ProjectAccess
from firefly_forge.runtime_roles import RuntimeRoles
from firefly_forge.scm import checked, clone
from firefly_forge.scm_natural_tasks import SCMNaturalTasks
from firefly_forge.scm_polling import SCMPolling
from firefly_forge.store import Store


def test_native_natural_checkpoint_captures_current_source_when_automatic_pause_checkpoint_is_off(
    native_poll, tmp_path, monkeypatch
):
    case = native_poll
    local = tmp_path / "recorded-checkpoint-source"
    local.mkdir()

    async def seed_local():
        (local / "product.py").write_text("value = 1\n")
        await checked(["git", "init", "--initial-branch=main"], local)
        await checked(["git", "add", "product.py"], local)
        await checked(
            [
                "git",
                "-c",
                "user.name=Public fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-m",
                "Public checkpoint baseline",
            ],
            local,
        )

    case.http.portal.call(seed_local)

    async def local_clone(repository, destination, branch, *, env=None, revision=None):
        assert repository["id"] == case.repo["id"] and repository["url"] == case.repo["url"]
        assert revision is None
        result = await clone({**repository, "url": str(local)}, destination, branch, env=env)
        await checked(["git", "remote", "set-url", "origin", repository["url"]], destination)
        return result

    api_store = case.store
    assert api_store.runtime_roles.role == "api"
    with pytest.raises(RuntimeError, match="API role"):
        api_store.runtime_roles.require_execution()
    worker = None
    runtime = None
    with monkeypatch.context() as patch:
        patch.setattr(engine_module, "clone", local_clone)

        async def start_worker():
            nonlocal worker, runtime
            await api_store.release_ownership()
            worker = Store(api_store.path)
            worker.configuration = api_store.configuration
            runtime = RuntimeRoles(worker, "worker")
            worker.runtime_roles = runtime
            await worker.initialize()
            await runtime.start()
            runtime.require_execution()
            await worker.acquire_ownership()
            worker.scm_polling = SCMPolling(worker, case.scm)
            worker.scm_polling.collaboration = case.collaboration
            create = worker.create_run

            async def scoped_run(body):
                assert body.repository_id == case.repo["id"]
                return await create(body.model_copy(update={"project_id": case.project["id"], "component_id": "api"}))

            patch.setattr(worker, "create_run", scoped_run)
            async with authenticated_operator(worker):
                await worker.update(case.run["id"], status="cancelled", worker_request=None)
                env = await paused(
                    worker,
                    tmp_path,
                    case.repo,
                    case.project,
                    on_pause=False,
                    workspaces=case.http.app.state.engine.workspaces,
                )
            case.run.clear()
            case.run.update(env.run)
            env.requests = ExecutionRequests(worker, runtime)
            env.tickets = GitDeliveryRequests(env.delivery, env.requests)
            await worker.release_ownership()
            await api_store.acquire_ownership()
            return env

        try:
            env = case.http.portal.call(start_worker)
            prepare(case, monkeypatch)
            case.process(case.observe("status"))
            case.state["natural_intent"] = "checkpoint"
            initial = case.process(observe(case, "@forge, save the current progress."))

            async def checkpoint_scope():
                async with authenticated_operator(case.store):
                    await ProjectAccess(case.store).set_membership(case.project["id"], case.mapped, "maintainer")
                natural = SCMNaturalTasks(case.collaboration)
                scopes = await natural.conversations.scopes(initial, remote=False)
                return await case.service._head(case.project["id"], case.repo["id"], scopes[0]["binding_key"])

            head = case.http.portal.call(checkpoint_scope)
            response = case.http.post(
                case.path + "/bindings",
                json={
                    **head["request"],
                    "request_id": uuid4().hex,
                    "expected_previous_digest": head["digest"],
                    "allowed_actions": ["respond", "status", "checkpoint"],
                },
            )
            assert response.status_code == 201, response.text
            settings = case.http.get(f"/api/v1/repositories/{case.repo['id']}/collaboration").json()
            settings = {key: value for key, value in settings.items() if key in CollaborationSettings.model_fields}
            settings["channel_commands"] = ["respond", "publish"]
            response = case.http.put(f"/api/v1/repositories/{case.repo['id']}/collaboration", json=settings)
            assert response.status_code == 200, response.text

            assert env.run["git_delivery_policy"]["on_pause"] is False
            assert not env.run.get("source_tree") and len(env.calls) == 1
            before = case.http.portal.call(env.journal.entries, env.run["id"])
            assert not any(entry["kind"].startswith("git_delivery_") for entry in before)
            baseline = env.run["base_revision"]
            index = env.workspace / ".git/index"
            index_before = hashlib.sha256(index.read_bytes()).hexdigest()
            assert case.http.portal.call(checked, ["git", "rev-parse", "HEAD"], env.workspace) == baseline
            assert (env.workspace / "product.py").read_text() == "value = 2\n"
            case.process(case.observe("status"))
            case.state["natural_intent"] = "checkpoint"
            message = observe(case, "@forge, save the current progress as a local checkpoint.")
            classified_before = case.state["natural_calls"]
            result = case.process(message)
            assert result["natural_turn"]["state"] == "checkpoint_queued", result
            marker = result["natural_checkpoint"]
            assert marker["request"]["expected_source_digest"]
            assert "expected_source_tree" not in marker["request"]
            request_id = marker["request"]["request_id"]
            assert hashlib.sha256(index.read_bytes()).hexdigest() == index_before
            assert case.http.portal.call(checked, ["git", "rev-parse", "HEAD"], env.workspace) == baseline

            async def consume():
                await api_store.release_ownership()
                await worker.acquire_ownership()
                poller = asyncio.create_task(env.requests._poll())
                try:
                    async with asyncio.timeout(20):
                        while True:
                            if poller.done():
                                await poller
                                raise AssertionError("The native owner poller stopped before acknowledging the request")
                            row = await env.requests.get(request_id)
                            if row["status"] not in {"queued", "executing"}:
                                return row
                            await asyncio.sleep(0.02)
                finally:
                    poller.cancel()
                    try:
                        await asyncio.gather(poller, *env.requests.active.values(), return_exceptions=True)
                    finally:
                        await worker.release_ownership()
                        await api_store.acquire_ownership()

            env.requests.git_deliveries = env.tickets
            completed = case.http.portal.call(consume)
            assert completed["status"] == "completed", completed
            revision = completed["result"]["revision"]
            assert case.http.portal.call(checked, ["git", "rev-parse", "HEAD^"], env.workspace) == baseline
            assert (
                case.http.portal.call(checked, ["git", "show", revision + ":product.py"], env.workspace) == "value = 2"
            )
            after = case.http.portal.call(env.journal.entries, env.run["id"])
            assert after[: len(before)] == before
            assert len([entry for entry in after if entry["kind"] == "git_delivery_intent"]) == 1
            assert len([entry for entry in after if entry["kind"] == "git_delivery_receipt"]) == 1
            assert env.journal.verify_chain(after)
            current = case.http.portal.call(worker.get_run, env.run["id"])
            assert current["status"] == "paused" and current["worker_request"] == env.run["worker_request"]
            assert len(env.calls) == 1
            repeated = case.process(message)
            assert repeated["natural_checkpoint"] == marker
            assert case.state["natural_calls"] == classified_before + 1
        finally:

            async def close_worker():
                try:
                    if runtime is not None:
                        await runtime.stop()
                finally:
                    try:
                        if worker is not None:
                            await worker.close()
                    finally:
                        await api_store.acquire_ownership()

            case.http.portal.call(close_worker)
