# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native API/Store/physical owner/Git receipts; only network transports are controlled."""

import asyncio
import copy
from contextvars import Context
from uuid import uuid4

import pytest
from checkpoint_test_native_fixtures import checkpoint_native as checkpoint_native  # noqa: F401
from scm_polling_native_fixtures import native_poll as native_poll  # noqa: F401
from worker_reply_fixtures import authenticated_operator

from firefly_forge import scm
from firefly_forge.execution_requests import ExecutionRequests
from firefly_forge.git_delivery_requests import GitDeliveryRequests


@pytest.mark.parametrize("checkpoint_native", ["unpublished"], indirect=True)
def test_native_new_remote_completion_preserves_unknown_ticket_and_selects_confirmed_source(
    checkpoint_native, monkeypatch
):
    c = checkpoint_native
    original_id, recovery_id = uuid4().hex, uuid4().hex
    run_id = c.parent["id"]
    push = scm.push

    async def source():
        async with authenticated_operator(c.store):
            _, _, tree = await c.engine._snapshot(
                c.engine.workspaces / run_id, c.parent["base_revision"], expected_head=c.parent["base_revision"]
            )
            return tree

    tree = c.case.http.portal.call(source)
    body = {
        "version": 1,
        "request_id": original_id,
        "expected_head": c.parent["base_revision"],
        "expected_source_tree": tree,
    }
    path = f"/api/v1/runs/{run_id}/git-deliveries"
    admitted = c.case.http.post(path, json=body)
    assert admitted.status_code == 202, admitted.text

    async def lost(*args, **kwargs):
        raise RuntimeError("Controlled interruption before remote dispatch")

    async def execute(request_id):
        tickets = GitDeliveryRequests(c.deliveries, ExecutionRequests(c.store, c.store.runtime_roles))
        await asyncio.create_task(tickets.execute(request_id), context=Context())

    monkeypatch.setattr(scm, "push", lost)
    c.case.http.portal.call(execute, original_id)
    monkeypatch.setattr(scm, "push", push)
    original = c.case.http.get(path + "/requests/" + original_id)
    assert original.status_code == 200 and original.json()["status"] == "needs_reconciliation"
    old = copy.deepcopy(original.json())
    observed = c.case.http.get(path + "/observations/" + original_id)
    assert observed.status_code == 200, observed.text
    observed = observed.json()
    review = observed["reconciliation_candidate"]
    assert observed["remote"]["observed_revision"] is None and c.draft_state["posts"] == 0

    async def before():
        async with authenticated_operator(c.store):
            return await c.store.get_run(run_id), await c.journal.entries(run_id)

    parent, prefix = c.case.http.portal.call(before)
    recovery = {
        "version": 1,
        "request_id": recovery_id,
        "expected_head": observed["observed_head"],
        "expected_source_tree": observed["observed_tree"],
        "explicit_reconciliation": {
            **review,
            "valid_for_seconds": 300,
            "acknowledge_original_outcome_unknown": True,
            "acknowledge_retained_body_only": True,
            "acknowledge_no_execution_grant": True,
        },
    }
    response = c.case.http.post(path, json=recovery)
    assert response.status_code == 202 and response.json()["status"] == "queued", response.text
    c.case.http.portal.call(execute, recovery_id)
    confirmed = c.case.http.get(path + "/requests/" + recovery_id)
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "completed", confirmed.text
    result = confirmed.json()["result"]
    assert result["revision"] == observed["observed_head"] and result["source_tree"] == tree
    assert c.case.http.get(path + "/requests/" + original_id).json() == old
    assert c.draft_state["posts"] == 1
    after, entries = c.case.http.portal.call(before)
    assert entries[: len(prefix)] == prefix
    presentation = {"updated_at", "git_delivery_summary", "git_delivery_draft", "events"}
    assert {k: v for k, v in after.items() if k not in presentation} == {
        k: v for k, v in parent.items() if k not in presentation
    }
    assert after["events"][:-1] == parent["events"]
    activity = after["events"][-1]
    assert activity["id"] not in {event["id"] for event in parent["events"]}
    assert activity["stage"] == "git_delivery"
    assert activity["message"] == f"Factory checkpoint 1: {result['revision']} (draft) {result['pr_url']}"
    assert sum(e["kind"] == "git_delivery_receipt" for e in entries) == 1
    remote = next(e for e in reversed(entries) if e["kind"] == "git_delivery_remote_receipt")
    assert remote["payload"]["reconciliation"]["original_intent_digest"] == review["remote_intent_digest"]
    from firefly_forge.scm_natural_delivery import delivery_facts

    facts = delivery_facts(after, entries)
    assert facts["checkpoint"]["revision"] == result["revision"]
    assert facts["remote"] == {"status": "draft", "pr_url": result["pr_url"]}
    assert facts["product_acceptance"] == "not_established_by_checkpoint"
    child = copy.deepcopy(c.body)
    child["input"]["source_checkpoint"] = {
        "version": 1,
        "run_id": run_id,
        "request_id": original_id,
        "expected_checkpoint_digest": review["checkpoint_digest"],
        "expected_revision": result["revision"],
    }
    created = c.case.http.post("/api/v1/run-creation-requests/" + uuid4().hex, json=child)
    assert created.status_code == 201, created.text
    assert created.json()["source_checkpoint"]["remote_receipt_digest"] == remote["digest"]
