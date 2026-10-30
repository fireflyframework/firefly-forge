# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Dedicated PostgreSQL/DI controls with recorded App HTTP; no live provider or model."""

import copy
import hashlib
import hmac
import json
from uuid import uuid4

import pytest
from natural_coordinator_model_fixture import native_coordinator
from scm_polling_native_fixtures import native_poll as native_poll
from worker_reply_fixtures import authenticated_operator

from firefly_forge.collaboration import CollaborationSettings
from firefly_forge.journal import digest
from firefly_forge.models import RunInput
from firefly_forge.scm_actor_authority import SCMActorAuthority
from firefly_forge.scm_authority_models import SCMActorBindingInputV1


def natural(c, body="@forge how is this going?", number=80, issue=3):
    c.state["comments"].append(c.comment(number, body, issue))
    c.state["clock"] += 3600
    for _ in range(2):
        c.http.portal.call(c.service.step, c.repo["id"])
    return next(row for row in c.http.portal.call(c.retained)[2] if row["verification"]["resource_id"] == str(number))


def test_native_natural_status_retains_real_ticket_context_and_is_inert_on_restart_replay(native_poll, monkeypatch):
    c = native_poll
    c.ready()
    c.consent()
    prior = c.process(c.observe("status"))
    coordinator_calls = native_coordinator(c, monkeypatch)
    before = c.http.portal.call(c.journal.entries, c.run["id"])
    delivery = natural(c)
    result = c.process(delivery)
    assert result["status"] == "processed" and result["run_id"] == c.run["id"]
    assert result["conversation"]["references"][0]["receipt_id"] == prior["id"]
    assert result["conversation"]["scopes"][0]["native_user_id"] == c.mapped.id.hex
    assert result["command"] == delivery["command"] and result["verification"] == delivery["verification"]
    assert result["source_snapshot"] == delivery["source_snapshot"]
    assert result["feedback_intent"]["body_sha256"] == hashlib.sha256(c.state["posts"][-1]["body"].encode()).hexdigest()
    assert c.http.portal.call(c.journal.entries, c.run["id"]) == before
    assert c.process(delivery) == result
    c.http.portal.call(c.collaboration.sync_feedback)
    assert len(c.state["posts"]) == 2
    assert len(coordinator_calls) == 1


def test_native_missing_ticket_context_never_selects_the_existing_repository_run(native_poll, monkeypatch):
    c = native_poll
    c.ready()
    c.consent()
    coordinator_calls = native_coordinator(c, monkeypatch)
    before = c.http.portal.call(c.journal.entries, c.run["id"])
    delivery = natural(c, "@forge ¿cómo va?")
    result = c.process(delivery)
    assert result["status"] == "processed" and result["run_id"] is None
    assert result["conversation"]["selection"] == "missing" and "ejecución" in c.state["posts"][0]["body"]
    assert c.run["id"] not in c.state["posts"][0]["body"]
    assert c.http.portal.call(c.journal.entries, c.run["id"]) == before
    assert len(coordinator_calls) == 1


def test_native_source_edit_during_issue_token_wait_stops_reply_and_preserves_unknown_hold(native_poll, monkeypatch):
    c = native_poll
    c.ready()
    c.consent()
    native_coordinator(c, monkeypatch)
    delivery = natural(c)
    prepare = c.scm.issue_authentication

    async def edited_after_token(*args, **kwargs):
        value = await prepare(*args, **kwargs)
        next(comment for comment in c.state["comments"] if comment["id"] == 80)["body"] += " changed"
        return value

    monkeypatch.setattr(c.scm, "issue_authentication", edited_after_token)
    result = c.process(delivery)
    assert result["action_status"] == "completed" and result["feedback_status"] == "needs_reconciliation"
    assert c.state["posts"] == []
    c.http.portal.call(c.collaboration.sync_feedback)
    assert c.state["posts"] == []


def test_native_signed_status_requires_actual_native_status_consent_and_retains_original_signature(
    native_poll, monkeypatch
):
    c = native_poll
    comment = c.comment(90, "@forge how is this going?")
    c.state["comments"].append(comment)
    settings = CollaborationSettings(
        enabled=True, trusted_actors=["7"], allowed_commands=["status"], webhook_secret="owned-signed-natural-fixture"
    )
    configuration = settings.model_dump(mode="json", exclude={"webhook_secret"})
    configuration["webhook_secret"] = "owned-signed-natural-fixture"
    configured = c.http.put(f"/api/v1/repositories/{c.repo['id']}/collaboration", json=configuration)
    assert configured.status_code == 200, configured.text
    raw = json.dumps(
        {
            "action": "created",
            "installation": {"id": 34},
            "repository": {"id": 42},
            "sender": {"id": 7, "type": "User"},
            "issue": {"number": 3},
            "comment": comment,
        }
    ).encode()
    headers = {
        "x-github-event": "issue_comment",
        "x-github-delivery": uuid4().hex,
        "x-hub-signature-256": "sha256=" + hmac.new(b"owned-signed-natural-fixture", raw, hashlib.sha256).hexdigest(),
    }
    delivery = c.http.portal.call(c.collaboration.receive, "github", c.repo["id"], raw, headers)
    original = copy.deepcopy(delivery)

    async def consent():
        async with authenticated_operator(c.store):
            authority = SCMActorAuthority(c.store, c.scm)
            project = await authority._project(c.project["id"], c.repo["id"])
            return await authority.configure(
                c.project["id"],
                c.repo["id"],
                SCMActorBindingInputV1(
                    version=1,
                    request_id=uuid4().hex,
                    expected_previous_digest=None,
                    expected_project_binding_digest=digest(project),
                    verified_receipt_id=delivery["id"],
                    expected_verified_reference_digest=digest(delivery["verification"]),
                    native_user_id=c.mapped.id.hex,
                    component_ids=["api"],
                    allowed_actions=["status"],
                    active=True,
                    acknowledge_webhook_asserted_identity=True,
                ),
            )

    bound = c.http.portal.call(consent)
    coordinator_calls = native_coordinator(c, monkeypatch)
    result = c.process(delivery)
    assert len(coordinator_calls) == 1
    assert result["status"] == "processed" and result["run_id"] is None
    assert result["conversation"]["scopes"][0]["binding_digest"] == bound["digest"]
    assert result["verification"] == original["verification"] and "transport" not in result["verification"]
    assert result["source_snapshot"] == original["source_snapshot"]
    assert len(c.state["posts"]) == 1


def test_native_multiple_active_ticket_runs_require_clarification_without_run_mutation(native_poll, monkeypatch):
    c = native_poll
    c.ready()
    c.consent()
    c.process(c.observe("status"))

    async def create():
        async with authenticated_operator(c.store):
            return await c.store.create_run(
                RunInput(
                    repository_id=c.repo["id"],
                    project_id=c.project["id"],
                    component_id="api",
                    operation="test",
                    agent="bridge",
                    frontend="none",
                    brief="Another independently linked controlled execution",
                )
            )

    other = c.http.portal.call(create)
    explicit = natural(c, "/forge status " + json.dumps({"run_id": other["id"]}), number=81)
    c.process(explicit)
    coordinator_calls = native_coordinator(c, monkeypatch)
    result = c.process(natural(c, number=82))
    assert result["status"] == "processed" and result["run_id"] is None
    assert result["conversation"]["selection"] == "ambiguous"
    assert len(c.state["posts"]) == 3 and "more than one" in c.state["posts"][-1]["body"].lower()
    assert c.http.portal.call(c.store.get_run, other["id"])["status"] == other["status"]
    assert len(coordinator_calls) == 1


@pytest.mark.parametrize("kind_changed", [False, True])
def test_native_addressed_pull_request_preserves_resource_kind_before_reply(native_poll, kind_changed):
    c = native_poll
    c.ready()
    c.consent()
    delivery = natural(c, issue=4)
    assert delivery["issue"]["kind"] == "pull_request"
    c.state["pr_kind_changed"] = kind_changed
    result = c.process(delivery)
    assert result["status"] == ("denied" if kind_changed else "processed")
    assert len(c.state["posts"]) == (0 if kind_changed else 1)
