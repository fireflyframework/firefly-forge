# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Read-only addressed comments use actual native polling consent, never fake runs."""

import copy
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_scm_polling import poll_case as poll_case

from firefly_forge.journal import digest
from firefly_forge.project_access import AccessDenied


async def addressed(c, body="@forge how is this going?", number=80, issue_number=3):
    c.state["clock"] += 3600
    comment = c.comment(number, body)
    comment["issue_url"] = "https://api.github.com/repos/team/product/issues/" + str(issue_number)
    c.state["comments"].append(comment)
    for _ in range(3):
        await c.service.step(c.repository)
    return next(copy.deepcopy(r.data) for r in c.db["inbox"].values() if r.source_key == "comment:" + str(number))


async def test_addressed_comment_retains_exact_observation_and_current_read_only_scope(poll_case):
    async with poll_case() as c:
        await c.ready()
        consent = await c.consent()
        delivery = await addressed(c)
        assert delivery["status"] == "received"
        assert delivery["command"]["adapter"] == "addressed_status_v1"
        assert "run_id" not in delivery["command"]
        assert digest(delivery["source_snapshot"]) == delivery["digest"] == delivery["verification"]["payload_digest"]
        scope = await c.service.conversation_scope(delivery)
        assert scope["project_id"] == c.project and scope["native_user_id"] == c.user_id
        assert scope["component_ids"] == ["api"] and scope["action"] == "status"
        assert scope["binding_digest"] == consent["digest"]
        assert scope["verified_receipt_id"] == delivery["id"]
        assert "run_id" not in scope and "scm_delegation" not in scope
        assert not c.db["journal"] and not c.state["posts"]


@pytest.mark.parametrize("change", ["membership", "native_user", "actor", "action", "edited", "consent"])
async def test_addressed_read_scope_refuses_current_revocation_without_reply_or_dispatch(poll_case, change):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        delivery = await addressed(c)
        assert delivery["status"] == "received"
        if change == "membership":
            c.db["memberships"]["member"].active = False
        elif change == "native_user":
            c.db["users"][c.user_id].enabled = False
        elif change == "actor":
            c.db["settings"][c.repository].data["trusted_actors"] = []
        elif change == "action":
            c.db["settings"][c.repository].data["allowed_commands"] = []
        elif change == "edited":
            c.state["edited"] = True
        else:
            request = c.state["consent_request"].model_copy(
                update={
                    "request_id": "d" * 32,
                    "expected_previous_digest": (await c.service.conversation_scope(delivery))["binding_digest"],
                    "active": False,
                }
            )
            await c.service.configure_binding(c.project, c.repository, request)
        with pytest.raises(AccessDenied):
            await c.service.conversation_scope(delivery)
        assert not c.db["journal"] and not c.state["posts"]


async def test_addressed_status_never_upgrades_absent_poll_consent(poll_case):
    async with poll_case() as c:
        await c.ready()
        delivery = await addressed(c)
        assert delivery["status"] == "received"
        with pytest.raises(AccessDenied):
            await c.service.conversation_scope(delivery)
        assert not c.state["posts"]


async def test_natural_status_links_api_created_run_through_native_processed_ticket_receipt(poll_case):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        explicit = await c.new_command("status")
        await c.collaboration.process(explicit["id"])
        assert "issue_context" not in c.db["runs"][c.run_id].data
        delivery = await addressed(c)
        result = await c.collaboration.process(delivery["id"])
        assert result["status"] == "processed" and result["run_id"] == c.run_id
        assert result["conversation"]["selection"] == "run"
        assert result["conversation"]["references"][0]["receipt_id"] == explicit["id"]
        assert result["source_snapshot"] == delivery["source_snapshot"]
        assert result["verification"] == delivery["verification"]
        assert result["command"] == delivery["command"]
        assert len(c.state["posts"]) == 2 and not c.db["journal"]
        await c.collaboration.process(delivery["id"])
        await c.collaboration.feedback(delivery["id"])
        assert len(c.state["posts"]) == 2


async def test_natural_status_without_ticket_context_explains_missing_link_without_latest_repo_fallback(poll_case):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        delivery = await addressed(c, "@forge ¿cómo va?")
        result = await c.collaboration.process(delivery["id"])
        assert result["status"] == "processed" and result["run_id"] is None
        assert result["conversation"]["selection"] == "missing"
        assert len(c.state["posts"]) == 1
        assert "ejecución" in c.state["posts"][0]["body"]
        assert c.run_id not in c.state["posts"][0]["body"]
        assert not c.db["journal"]


async def test_natural_status_asks_when_two_active_runs_are_authentically_linked_to_ticket(poll_case):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        first = await c.new_command("status")
        await c.collaboration.process(first["id"])
        other = uuid4().hex
        run = copy.deepcopy(c.db["runs"][c.run_id].data)
        run["id"] = other
        c.db["runs"][other] = SimpleNamespace(
            id=UUID(other), repository_id=UUID(c.repository), status=run["status"], data=run
        )
        second = await addressed(c, "/forge status " + json.dumps({"run_id": other}), 81)
        await c.collaboration.process(second["id"])
        delivery = await addressed(c, number=82)
        result = await c.collaboration.process(delivery["id"])
        assert result["status"] == "processed" and result["run_id"] is None
        assert result["conversation"]["selection"] == "ambiguous"
        assert len(c.state["posts"]) == 3
        assert "more than one" in c.state["posts"][-1]["body"].lower()
        assert not c.db["journal"]


async def test_terminal_ticket_context_uses_latest_explicit_receipt_not_run_update_time(poll_case):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        c.db["runs"][c.run_id].data.update(status="failed", updated_at="2099-01-01T00:00:00Z")
        first = await c.new_command("status")
        await c.collaboration.process(first["id"])
        other = uuid4().hex
        run = copy.deepcopy(c.db["runs"][c.run_id].data)
        run.update(id=other, status="cancelled", updated_at="2000-01-01T00:00:00Z")
        c.db["runs"][other] = SimpleNamespace(
            id=UUID(other), repository_id=UUID(c.repository), status=run["status"], data=run
        )
        second = await addressed(c, "/forge status " + json.dumps({"run_id": other}), 81)
        await c.collaboration.process(second["id"])
        result = await c.collaboration.process((await addressed(c, number=82))["id"])
        assert result["conversation"]["selection"] == "run" and result["run_id"] == other
        assert result["conversation"]["references"][0]["receipt_id"] == second["id"]
        assert len(c.state["posts"]) == 3 and not c.db["journal"]


@pytest.mark.parametrize("kind_changed", [False, True])
async def test_natural_status_preserves_and_rechecks_exact_pull_request_kind(poll_case, kind_changed):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        c.state["extra_issues"] = {"4": {"number": 4, "id": 400, "pull_request": {}}}
        delivery = await addressed(c, issue_number=4)
        assert delivery["status"] == "received" and delivery["issue"]["kind"] == "pull_request"
        assert delivery["verification"]["issue_number"] == 4
        if kind_changed:
            del c.state["extra_issues"]["4"]["pull_request"]
        result = await c.collaboration.process(delivery["id"])
        assert result["status"] == ("denied" if kind_changed else "processed")
        assert len(c.state["posts"]) == (0 if kind_changed else 1)


async def test_new_read_only_pull_request_adapter_does_not_admit_legacy_commands(poll_case):
    async with poll_case() as c:
        await c.ready()
        await c.consent()
        c.state["extra_issues"] = {"4": {"number": 4, "id": 400, "pull_request": {}}}
        delivery = await addressed(c, "/forge status " + json.dumps({"run_id": c.run_id}), issue_number=4)
        assert delivery["status"] == "ignored" and delivery["reason"] == "unsupported_poll_issue"
        assert not c.state["posts"]
