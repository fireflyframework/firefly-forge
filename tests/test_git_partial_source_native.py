# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual PostgreSQL/current authority/real Git; model HTTP stays in memory."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from contextvars import Context
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest
from pydantic_ai import models
from test_agentic_checkpoint_activation_host import result
from test_bridge_completion_callbacks import CAPABILITY
from test_bridge_completion_native import mock_model_requests
from test_git_delivery_native import source_project
from worker_reply_fixtures import authenticated_operator

from firefly_forge.agent_roles import ProjectRoles, RoleRegistry
from firefly_forge.agentic_checkpoint_boundary import AgenticBoundary
from firefly_forge.configuration import ProviderConfiguration
from firefly_forge.engine import Engine
from firefly_forge.execution_requests import ExecutionRequests
from firefly_forge.git_deliveries import GitDeliveries
from firefly_forge.git_delivery_controllers import GitDeliveryController
from firefly_forge.git_delivery_models import GitDeliveryInputV1, GitDeliveryPolicyInputV1
from firefly_forge.git_delivery_policy import GitDeliveryPolicies
from firefly_forge.git_delivery_requests import GitDeliveryRequests
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.models import RunInput
from firefly_forge.providers import BridgeProvider
from firefly_forge.runtime_roles import RuntimeRoles
from firefly_forge.scm import checked
from firefly_forge.store import identifier


@asynccontextmanager
async def native_partial(store, root, monkeypatch):
    async with authenticated_operator(store):
        runtime = RuntimeRoles(store, "combined")
        store.runtime_roles = runtime
        await runtime.start()
        await store.acquire_ownership()
        model = None
        try:
            repository, project = await source_project(store, root)
            await GitDeliveryPolicies(store).configure(
                project["id"],
                GitDeliveryPolicyInputV1(
                    version=1,
                    request_id=uuid4().hex,
                    expected_previous_digest=None,
                    policy={"enabled": True, "remote": "local"},
                ),
            )
            store.configuration.update(
                "bridge",
                ProviderConfiguration(
                    values={
                        "endpoint": "http://127.0.0.1:19191",
                        "token": "fictional-partial-source-token",
                        "completion_recovery": "required",
                        "checkpoints": "required",
                    }
                ),
            )
            run = await store.create_run(
                RunInput(
                    repository_id=repository["id"],
                    project_id=project["id"],
                    component_id="core",
                    agent="bridge",
                    frontend="none",
                    skills=[],
                    brief="Retain a public unverified source frontier.",
                )
            )
            await store.update(
                run["id"],
                repair_budget=0,
                role_snapshot=RoleRegistry(root / "roles").snapshot(
                    ProjectRoles(disabled=["docs"], allowed_tools={"implementer": ["write_file", "read_file"]})
                ),
            )
            journal = Journal(store)
            provider = BridgeProvider(
                store.configuration.load_snapshot((await store.get_run(run["id"]))["config_revision"])
            )
            model = provider.model()
            monkeypatch.setattr(provider, "model", lambda **kwargs: model)
            engine = Engine(store, root / "workspaces", lambda _: provider, journal=journal)
            engine.enforce_ownership = True
            delivery = GitDeliveries(store, engine, journal)
            engine.git_deliveries = delivery
            captures, posts = 0, []
            original = AgenticBoundary.capture

            def capture(boundary, wire):
                nonlocal captures
                captures += 1
                if captures == 3:
                    raise ValueError("Controlled pre-dispatch checkpoint refusal")
                return original(boundary, wire)

            monkeypatch.setattr(AgenticBoundary, "capture", capture)

            def transport(request):
                if request.method == "GET":
                    return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
                posts.append(request.content)
                assert len(posts) <= 2
                body = result(tool=True)
                call = body["choices"][0]["message"]["tool_calls"][0]
                call["id"] = "partial-" + str(len(posts))
                call["function"] = (
                    {"name": "write_file", "arguments": json.dumps({"path": "product.py", "content": "value = 2\n"})}
                    if len(posts) == 1
                    else {"name": "read_file", "arguments": json.dumps({"path": "product.py"})}
                )
                return httpx2.Response(200, json=body)

            model.client._client._transport = httpx2.MockTransport(transport)
            model.client._client._mounts = {}
            engine.active[run["id"]] = asyncio.current_task()
            try:
                with models.override_allow_model_requests(False), mock_model_requests(model):
                    await engine.execute_run(run["id"])
            finally:
                engine.active.pop(run["id"], None)
            failed = await store.get_run(run["id"])
            assert failed["status"] == "failed", failed.get("error")
            assert failed["implementation_pending"] and failed["bridge_completion_hold"]
            entries = await journal.entries(run["id"])
            assert sum(e["kind"] == "agent_consume" for e in entries) == len(posts) == 2
            requests = ExecutionRequests(store, runtime)
            tickets = GitDeliveryRequests(delivery, requests)
            controller = GitDeliveryController(delivery, tickets)
            candidate = await controller.partial_source_candidate(run["id"])
            body = GitDeliveryInputV1.model_validate(
                dict(
                    version=1,
                    request_id=uuid4().hex,
                    expected_head=candidate["expected_head"],
                    expected_source_digest=candidate["source_digest"],
                    explicit_partial_source={
                        "version": 1,
                        "candidate_digest": candidate["digest"],
                        **{
                            key: candidate[key]
                            for key in [
                                "policy_digest",
                                "expected_run_digest",
                                "journal_digest",
                                "source_digest",
                                "frontier_record_digest",
                            ]
                        },
                        "acknowledge_partial_unverified": True,
                        "acknowledge_preserve_failed_graph": True,
                        "acknowledge_no_execution_grant": True,
                    },
                )
            )
            yield SimpleNamespace(
                store=store,
                engine=engine,
                journal=journal,
                delivery=delivery,
                tickets=tickets,
                controller=controller,
                run_id=run["id"],
                workspace=engine.workspaces / run["id"],
                candidate=candidate,
                body=body,
                parent=copy.deepcopy(failed),
                prefix=copy.deepcopy(entries),
                posts=posts,
            )
        finally:
            if model is not None:
                await model.client.close()
            await runtime.stop()
            await store.release_ownership()


async def test_native_partial_source_queue_commits_without_resuming_or_resetting_parent(store, tmp_path, monkeypatch):
    async with native_partial(store, tmp_path, monkeypatch) as c:
        queued = await c.controller.partial_source_request(c.run_id, c.body)
        assert queued["status"] == "queued"
        await asyncio.create_task(c.tickets.execute(c.body.request_id), context=Context())
        receipt = await c.controller.ticket(c.run_id, c.body.request_id)
        assert receipt["status"] == "completed", receipt.get("result")
        assert receipt["result"]["verification_status"] == "partial_unverified"
        assert await checked(["git", "show", "HEAD:product.py"], c.workspace) == "value = 2"
        assert (await checked(["git", "log", "-1", "--format=%s"], c.workspace)).startswith("[UNVERIFIED]")
        after = await store.get_run(c.run_id)
        for key in [
            "status",
            "active_task",
            "implementation_pending",
            "bridge_completion_hold",
            "implementation_rounds",
            "attempt",
        ]:
            assert after.get(key) == c.parent.get(key)
        entries = await c.journal.entries(c.run_id)
        assert entries[: len(c.prefix)] == c.prefix and c.journal.verify_chain(entries)
        assert sum(e["kind"] == "agent_consume" for e in entries) == len(c.posts) == 2
        assert await c.controller.partial_source_request(c.run_id, c.body) == receipt
        await c.tickets.execute(c.body.request_id)
        assert await c.controller.ticket(c.run_id, c.body.request_id) == receipt


@pytest.mark.parametrize("drift", ["source", "authority", "ownership"])
async def test_native_partial_source_late_change_has_no_git_effect(store, tmp_path, monkeypatch, drift):
    async with native_partial(store, tmp_path, monkeypatch) as c:
        await c.controller.partial_source_request(c.run_id, c.body)
        if drift == "source":
            (c.workspace / "product.py").write_text("value = 99\n")
        elif drift == "authority":
            async with store._unit():
                repo = ForgeIdentityUserRepository()
                user = await repo.find_by_id(identifier(c.candidate["actor"]["user_id"]))
                user.enabled = False
                await repo.save(user)
        else:
            await store.release_ownership()
        work = asyncio.create_task(c.tickets.execute(c.body.request_id), context=Context())
        if drift == "ownership":
            with pytest.raises(RuntimeError, match="ownership"):
                await work
        else:
            await work
        assert await checked(["git", "rev-parse", "HEAD"], c.workspace) == c.candidate["expected_head"]
        async with store._unit():
            rows = await c.journal.records.find_for_run(identifier(c.run_id))
        assert not any(r.kind in {"git_partial_source_checkpoint", "git_delivery_intent"} for r in rows)


async def test_native_partial_admission_receipt_and_marker_roll_back_together(store, tmp_path, monkeypatch):
    async with native_partial(store, tmp_path, monkeypatch) as c:
        await c.controller.partial_source_request(c.run_id, c.body)
        append = c.journal._append

        async def fail(run_id, task, key, kind, payload):
            result = await append(run_id, task, key, kind, payload)
            if kind == "git_partial_source_checkpoint":
                raise RuntimeError("Controlled native refusal after partial receipt append")
            return result

        monkeypatch.setattr(c.journal, "_append", fail)
        await asyncio.create_task(c.tickets.execute(c.body.request_id), context=Context())
        after = await store.get_run(c.run_id)
        assert not after.get("git_partial_source_checkpoint")
        assert await c.journal.entries(c.run_id) == c.prefix
        assert await checked(["git", "rev-parse", "HEAD"], c.workspace) == c.candidate["expected_head"]
        assert (await c.controller.ticket(c.run_id, c.body.request_id))["status"] == "failed"


async def test_native_partial_unknown_commit_keeps_intent_and_does_not_reexecute(store, tmp_path, monkeypatch):
    import firefly_forge.git_deliveries as module

    async with native_partial(store, tmp_path, monkeypatch) as c:
        await c.controller.partial_source_request(c.run_id, c.body)
        command = module.checked

        async def lost(argv, *args, **kwargs):
            result = await command(argv, *args, **kwargs)
            if "commit" in argv:
                raise OSError("Controlled lost response after actual native Git commit")
            return result

        monkeypatch.setattr(module, "checked", lost)
        await asyncio.create_task(c.tickets.execute(c.body.request_id), context=Context())
        receipt = await c.controller.ticket(c.run_id, c.body.request_id)
        assert receipt["status"] == "needs_reconciliation"
        head = await checked(["git", "rev-parse", "HEAD"], c.workspace)
        assert head != c.candidate["expected_head"]
        assert await checked(["git", "rev-parse", "HEAD^"], c.workspace) == c.candidate["expected_head"]
        assert await c.controller.partial_source_request(c.run_id, c.body) == receipt
        await c.tickets.execute(c.body.request_id)
        assert await checked(["git", "rev-parse", "HEAD"], c.workspace) == head
        async with store._unit():
            rows = await c.journal.records.find_for_run(identifier(c.run_id))
        assert sum(r.kind == "git_delivery_intent" for r in rows) == 1
        assert not any(r.kind == "git_delivery_receipt" for r in rows)


async def test_native_partial_delivery_projection_requires_exact_retained_receipts(store, tmp_path, monkeypatch):
    from firefly_forge.git_partial_source_checkpoints import GitPartialSourceCheckpoints

    async with native_partial(store, tmp_path, monkeypatch) as c:
        await c.controller.partial_source_request(c.run_id, c.body)
        await asyncio.create_task(c.tickets.execute(c.body.request_id), context=Context())
        receipt = await c.controller.ticket(c.run_id, c.body.request_id)
        assert receipt["status"] == "completed", receipt.get("result")
        current = await store.get_run(c.run_id)
        original = copy.deepcopy(current["git_delivery_summary"])
        service = GitPartialSourceCheckpoints(c.delivery)
        await service.guard(current, current["execution_actor"])
        await store.update(c.run_id, git_delivery_summary={**original, "revision": "f" * 40})
        with pytest.raises(ValueError, match="projection"):
            await service.guard(await store.get_run(c.run_id), current["execution_actor"])
        await store.update(
            c.run_id, git_delivery_summary=original, git_delivery_draft={"url": "https://example.test/forged"}
        )
        with pytest.raises(ValueError, match="projection"):
            await service.guard(await store.get_run(c.run_id), current["execution_actor"])
        await store.update(c.run_id, git_delivery_draft=None)
        await service.guard(await store.get_run(c.run_id), current["execution_actor"])
        after = await store.get_run(c.run_id)
        assert (
            after["status"] == c.parent["status"]
            and after["bridge_completion_hold"] == c.parent["bridge_completion_hold"]
        )
        assert sum(e["kind"] == "agent_consume" for e in await c.journal.entries(c.run_id)) == len(c.posts) == 2
