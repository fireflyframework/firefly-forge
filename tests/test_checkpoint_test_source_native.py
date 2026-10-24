# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared native API/worker cases; no live provider, model, or original CRM."""

import asyncio
import copy
from uuid import uuid4

import pytest
from checkpoint_test_native_fixtures import checkpoint_native as checkpoint_native  # noqa: F401
from pyfly.data.relational.sqlalchemy import LockMode
from scm_polling_native_fixtures import native_poll as native_poll  # noqa: F401
from test_addressed_status_native import natural
from worker_reply_fixtures import authenticated_operator

from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.project_access import ProjectAccess
from firefly_forge.run_creation_requests import RunCreationRequests
from firefly_forge.sandbox_models import validate_sandbox_snapshot
from firefly_forge.scm_conversations import SCMConversations


def test_native_checkpoint_fresh_same_project_test_seven_checks_and_teammate_read_lineage(checkpoint_native):
    c = checkpoint_native
    request_id = uuid4().hex
    path = "/api/v1/run-creation-requests/" + request_id
    legacy = c.case.http.post("/api/v1/runs", json=c.body["input"])
    assert legacy.status_code == 409 and "request_run_creation" in legacy.text
    created = c.case.http.post(path, json=c.body)
    assert created.status_code == 201, created.text
    receipt = created.json()
    assert receipt["request_id"] == request_id and receipt["run_id"] != c.parent["id"]
    assert receipt["source_checkpoint"]["revision"] == c.checkpoint["revision"]
    assert c.case.http.get(path).json() == receipt
    assert c.case.http.post(path, json=c.body).status_code == 200

    async def api_lease():
        await c.store.release_ownership()
        await c.case.store.acquire_ownership()

    c.case.http.portal.call(api_lease)
    c.case.ready()
    c.case.consent()
    c.case.run.clear()
    c.case.run.update(c.parent)
    parent_status = c.case.process(c.case.observe("status"))
    assert parent_status["status"] == "processed" and parent_status["run_id"] == c.parent["id"]

    observation = natural(c.case)
    assert observation["id"] != parent_status["id"] and observation["command"]["adapter"] == "addressed_status_v1"

    async def lineage():
        service = SCMConversations(c.case.collaboration)
        scopes = await service.scopes(observation, remote=False)
        refs, runs = await service.references(observation, scopes)
        child_ref = next(ref for ref in refs if ref["run_id"] == receipt["run_id"])
        assert child_ref["receipt_id"] == request_id
        assert child_ref["kind"] == "native_checkpoint_test"
        assert child_ref["creator_user_id"] == receipt["original_actor"]["user_id"]
        assert child_ref["creator_user_id"] != scopes[0]["native_user_id"]
        assert runs[receipt["run_id"]]["operation"] == "test"
        async with authenticated_operator(c.store):
            await ProjectAccess(c.store).set_membership(c.case.project["id"], c.case.mapped, None)
        refs, _ = await service.references(observation, scopes)
        assert not any(ref["run_id"] == receipt["run_id"] for ref in refs)
        await c.case.store.release_ownership()
        await c.store.acquire_ownership()

    c.case.http.portal.call(lineage)

    async def execute():
        child = await c.store.get_run(receipt["run_id"])
        assert all(child[key] == c.parent[key] for key in ("repository_id", "project_id", "component_id"))
        assert child["execution_actor"]["user_id"] == receipt["original_actor"]["user_id"]
        assert not child.get("provider_recovery") and not child.get("worker_response")
        assert child["publish"] is False and child["operation"] == "test"
        assert (
            child["commands"] == c.commands and validate_sandbox_snapshot(child["sandbox_snapshot"]).command_limit == 7
        )
        c.engine.active[child["id"]] = asyncio.current_task()
        try:
            await c.engine.execute_run(child["id"])
        finally:
            c.engine.active.pop(child["id"], None)
        final = await c.store.get_run(child["id"])
        assert final["status"] == "succeeded", final.get("error")
        assert final["base_revision"] == final["checkpoint_revision"] == c.checkpoint["revision"]
        assert final["source_tree"] == c.checkpoint["source_tree"]
        assert len(final["final_verification"]) == 7
        assert [check["argv"] for check in final["final_verification"]] == [cmd["argv"] for cmd in c.commands]
        assert all(check["exit_code"] == 0 for check in final["final_verification"])
        executions = await c.journal.sandbox_executions(child["id"])
        assert len(executions) == 7 and sorted(row["ordinal"] for row in executions) == list(range(1, 8))
        assert all(
            row["task_id"] == "verify" and row["snapshot_digest"] == final["sandbox_snapshot"]["digest"]
            for row in executions
        )
        assert all(
            row["effects"]["start"]["status"] == "done" and row["effects"]["start"]["evidence"]["exit_code"] == 0
            for row in executions
        )
        assert (await c.store.repository(c.case.repo["id"]))["default_branch"] == "main"
        assert await c.store.get_run(c.parent["id"]) == c.parent
        assert await c.journal.entries(c.parent["id"]) == c.prefix
        assert c.draft_state["posts"] == 1
        entries = await c.journal.entries(child["id"])
        assert c.journal.verify_chain(entries)
        assert not any(e["kind"] == "git_delivery_remote_intent" for e in entries)

    c.case.http.portal.call(execute)


def test_native_checkpoint_source_revocation_stale_revision_and_remote_move_never_queue(checkpoint_native):
    c = checkpoint_native
    original = copy.deepcopy(c.body)
    bad = copy.deepcopy(original)
    bad["input"]["source_checkpoint"]["expected_revision"] = "0" * 40
    assert c.case.http.post("/api/v1/run-creation-requests/" + uuid4().hex, json=bad).status_code == 409

    async def move():
        await c.original_checked(["git", "checkout", c.checkpoint["branch"]], c.origin)
        (c.origin / "product.py").write_text("value = 3\n")
        await c.original_checked(
            [
                "git",
                "-c",
                "user.name=Public fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-am",
                "A later remote source",
            ],
            c.origin,
        )

    c.case.http.portal.call(move)
    assert c.case.http.post("/api/v1/run-creation-requests/" + uuid4().hex, json=original).status_code == 409

    async def disable():
        async with c.store._unit():
            users = ForgeIdentityUserRepository()
            user = await users.find_by_id(c.case.owner.id, lock=LockMode.PESSIMISTIC_WRITE)
            assert user is not None
            user.enabled = False
            await users.save(user)

    c.case.http.portal.call(disable)
    assert c.case.http.post("/api/v1/run-creation-requests/" + uuid4().hex, json=original).status_code == 401
    assert all(run["id"] in {c.parent["id"], c.case.run["id"]} for run in c.case.http.portal.call(c.store.runs))


def test_native_checkpoint_creation_receipt_failure_rolls_back_child_without_touching_parent(
    checkpoint_native, monkeypatch
):
    from firefly_forge.run_creation_models import RunCreationInput

    c = checkpoint_native

    async def rollback():
        from firefly_forge.checkpoint_test_sources import CheckpointTestSources

        async with authenticated_operator(c.store):
            manager = RunCreationRequests(c.store, checkpoint_sources=CheckpointTestSources(c.deliveries))
            save = manager.records.save

            async def refuse(row):
                await save(row)
                raise ValueError("Controlled atomic creation receipt rollback")

            monkeypatch.setattr(manager.records, "save", refuse)
            before = await c.store.runs()
            with pytest.raises(ValueError, match="receipt rollback"):
                await manager.admit(uuid4().hex, RunCreationInput.model_validate(c.body))
            assert await c.store.runs() == before
            assert await c.store.get_run(c.parent["id"]) == c.parent
            assert await c.journal.entries(c.parent["id"]) == c.prefix

    c.case.http.portal.call(rollback)
