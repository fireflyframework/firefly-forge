# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One prepared actual-Store/Git maintain unit; seeded parent budget is fixture history."""

import asyncio
import copy
import json
import sys
from uuid import NAMESPACE_URL, uuid4, uuid5

from checkpoint_test_native_fixtures import checkpoint_native as checkpoint_native  # noqa: F401
from scm_polling_native_fixtures import native_poll as native_poll  # noqa: F401
from test_addressed_status_native import natural
from worker_reply_fixtures import authenticated_operator

from firefly_forge.git_deliveries import delivery_digest
from firefly_forge.git_delivery_models import GitDeliveryPolicyInputV1
from firefly_forge.git_delivery_policy import GitDeliveryPolicies
from firefly_forge.project_access import ProjectAccess
from firefly_forge.providers import AgentResult
from firefly_forge.scm_conversations import SCMConversations
from firefly_forge.store import identifier
from firefly_forge.tool_persistence import ForgeToolInvocationEntity, ForgeToolInvocationRepository


def test_native_maintain_checkpoint_uses_fresh_unit_without_recycling_failed_parent_budget(checkpoint_native):
    c = checkpoint_native

    async def retained_tools():
        async with c.store._unit():
            return [
                dict(
                    id=row.id.hex,
                    run_id=row.run_id.hex,
                    task_id=row.task_id,
                    correlation_id=row.correlation_id,
                    tool=row.tool,
                    status=row.status,
                    data=copy.deepcopy(row.data),
                )
                for row in await ForgeToolInvocationRepository().for_run(identifier(c.parent["id"]))
            ]

    async def parent_history():
        async with authenticated_operator(c.store):
            current = await GitDeliveryPolicies(c.store).view(c.case.project["id"])
            await GitDeliveryPolicies(c.store).configure(
                c.case.project["id"],
                GitDeliveryPolicyInputV1(
                    version=1,
                    request_id=uuid4().hex,
                    expected_previous_digest=current["digest"],
                    policy={**current["policy"], "enabled": False},
                ),
            )
            await c.store.finish(c.parent["id"], "failed", error="Retained fixture unit has spent its own allowance")
            records = ForgeToolInvocationRepository()
            async with c.store._unit():
                for index in range(30):
                    correlation = uuid5(NAMESPACE_URL, f"retained-parent-budget:{c.parent['id']}:{index}")
                    tool = "read_file" if index < 20 else "list_files"
                    # Seeded prior history tests run-ID separation; this is not a measured tool/provider outcome.
                    await records.save(
                        ForgeToolInvocationEntity(
                            id=correlation,
                            run_id=identifier(c.parent["id"]),
                            task_id="implement",
                            tool=tool,
                            correlation_id=correlation.hex,
                            status="succeeded",
                            data={"fixture_history": True, "ordinal": index, "tool": tool},
                        )
                    )
            prefix = await retained_tools()
            parent = await c.store.get_run(c.parent["id"])
            journal = await c.journal.entries(c.parent["id"])
            assert len(prefix) == 30 and sum(row["tool"] == "read_file" for row in prefix) == 20
            return parent, journal, prefix

    c.parent, parent_journal, budget_prefix = c.case.http.portal.call(parent_history)
    body = copy.deepcopy(c.body)
    body["input"]["operation"] = "maintain"
    body["input"]["brief"] = "Continue the retained source in a distinct ordinary work unit"
    body["input"]["commands"] = [
        {"argv": [sys.executable, "-c", f"from product import value; assert value == 3; print({i})"], "timeout": 30}
        for i in range(1, 8)
    ]
    request_id = uuid4().hex
    path = "/api/v1/run-creation-requests/" + request_id
    created = c.case.http.post(path, json=body)
    assert created.status_code == 201, created.text
    receipt = created.json()
    assert receipt["source_checkpoint"]["operation"] == "maintain"
    assert receipt["source_checkpoint"]["revision"] == c.checkpoint["revision"]
    assert c.case.http.get(path).json() == receipt
    assert c.case.http.post(path, json=body).status_code == 200
    assert receipt["run_id"] != c.parent["id"]
    calls = []

    class MaintainWorker:
        async def execute(self, workspace, brief, *args):
            child = await c.store.get_run(receipt["run_id"])
            assert workspace.name == child["id"] != c.parent["id"]
            assert "Current implementation attempt: 1." in brief
            assert child["base_revision"] == c.checkpoint["revision"]
            assert (workspace / "product.py").read_text() == "value = 2\n"
            assert child.get("implementation_rounds", 0) == 0
            assert child["error"] is None
            assert not child.get("git_delivery_policy", {}).get("enabled")
            assert (
                not {"source_edit_continuation", "source_budget_allowance", "worker_response", "provider_recovery"}
                & child.keys()
            )
            async with c.store._unit():
                assert await ForgeToolInvocationRepository().for_run(identifier(child["id"])) == []
            calls.append(workspace)
            (workspace / "product.py").write_text("value = 3\n")
            feedback = {
                "id": "fresh-maintain-feedback",
                "role_id": "implementer",
                "task_id": "implement",
                "attempt": 1,
                "disposition": "completed",
                "summary": "A separate implementation unit changed the exact checkpoint source",
                "evidence_paths": ["product.py"],
            }
            return AgentResult(json.dumps({"summary": feedback["summary"], "feedback": feedback}), "controlled fixture")

    c.engine.provider_factory = lambda _: MaintainWorker()

    async def execute():
        child = await c.store.get_run(receipt["run_id"])
        assert child["execution_actor"]["user_id"] == receipt["original_actor"]["user_id"]
        assert child["publish"] is False and child["operation"] == "maintain"
        c.engine.active[child["id"]] = asyncio.current_task()
        try:
            await c.engine.execute_run(child["id"])
        finally:
            c.engine.active.pop(child["id"], None)
        final = await c.store.get_run(child["id"])
        assert final["status"] == "succeeded", final.get("error")
        assert final["implementation_rounds"] == 1 and len(calls) == 1
        assert final["base_revision"] == c.checkpoint["revision"]
        assert final["checkpoint_revision"] != c.checkpoint["revision"]
        assert len(final["final_verification"]) == 7
        assert all(check["exit_code"] == 0 for check in final["final_verification"])
        assert final["commands"] == body["input"]["commands"]
        assert await c.store.get_run(c.parent["id"]) == c.parent
        assert await c.journal.entries(c.parent["id"]) == parent_journal
        retained = await retained_tools()
        assert retained == budget_prefix and delivery_digest(retained) == delivery_digest(budget_prefix)
        assert (await c.store.repository(c.case.repo["id"]))["default_branch"] == "main"
        assert c.draft_state["posts"] == 1
        await c.store.release_ownership()
        await c.case.store.acquire_ownership()

    c.case.http.portal.call(execute)
    c.case.ready()
    c.case.consent()
    c.case.run.clear()
    c.case.run.update(c.parent)
    status = c.case.process(c.case.observe("status"))
    assert status["status"] == "processed" and status["run_id"] == c.parent["id"]
    observation = natural(c.case)
    assert observation["id"] != status["id"] and observation["command"]["adapter"] == "addressed_status_v1"

    async def lineage():
        try:
            conversations = SCMConversations(c.case.collaboration)
            scopes = await conversations.scopes(observation, remote=False)
            references, runs = await conversations.references(observation, scopes)
            reference = next(row for row in references if row["run_id"] == receipt["run_id"])
            assert reference["kind"] == "native_checkpoint_maintain"
            assert runs[receipt["run_id"]]["operation"] == "maintain"
            assert reference["receipt_id"] == request_id
            assert reference["creator_user_id"] == receipt["original_actor"]["user_id"]
            async with authenticated_operator(c.store):
                await ProjectAccess(c.store).set_membership(c.case.project["id"], c.case.mapped, None)
            references, _ = await conversations.references(observation, scopes)
            assert not any(row["run_id"] == receipt["run_id"] for row in references)
        finally:
            await c.case.store.release_ownership()
            await c.store.acquire_ownership()

    c.case.http.portal.call(lineage)
