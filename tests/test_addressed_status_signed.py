# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Signed human-comment snapshots preserve native verification and read-only scope."""

import hashlib
import hmac
import json
from uuid import uuid4

import pytest
from test_scm_polling import poll_case as poll_case

from firefly_forge.collaboration import CollaborationSettings
from firefly_forge.journal import digest
from firefly_forge.scm_authority_models import SCMActorBindingInputV1


async def signed_address(c, *, actor_type="User", issue_kind="issue", body="@forge how is this going?", number=90):
    if not c.state.get("signed_settings"):
        await c.collaboration.configure(
            c.repository,
            CollaborationSettings(
                enabled=True,
                trusted_actors=["7"],
                allowed_commands=["status"],
                webhook_secret="signed-status-fixture",
            ),
        )
        c.state["signed_settings"] = True
    comment = c.comment(number, body)
    comment["user"]["type"] = actor_type
    raw = json.dumps(
        {
            "action": "created",
            "installation": {"id": 34},
            "repository": {"id": 42},
            "sender": {"id": 7, "type": actor_type},
            "issue": {"number": 3, **({"pull_request": {}} if issue_kind == "pull_request" else {})},
            "comment": comment,
        }
    ).encode()
    headers = {
        "x-github-event": "issue_comment",
        "x-github-delivery": uuid4().hex,
        "x-hub-signature-256": "sha256=" + hmac.new(b"signed-status-fixture", raw, hashlib.sha256).hexdigest(),
    }
    return await c.collaboration.receive("github", c.repository, raw, headers), comment


async def test_signed_address_retains_original_human_source_beside_unchanged_verified_reference(poll_case):
    async with poll_case() as c:
        delivery, comment = await signed_address(c)
        assert delivery["status"] == "received"
        assert delivery["command"]["adapter"] == "addressed_status_v1"
        assert delivery["source_snapshot"]["body"] == comment["body"]
        assert delivery["source_snapshot"]["author_id"] == "7"
        assert delivery["verification"]["verification_method"] == "raw_body_hmac_sha256"
        assert "transport" not in delivery["verification"]
        assert delivery["addressed_source"]["payload_digest"] == delivery["verification"]["payload_digest"]
        assert not c.db["journal"] and not c.state["posts"]


async def test_signed_bot_address_is_not_a_human_read_request(poll_case):
    async with poll_case() as c:
        delivery, _ = await signed_address(c, actor_type="Bot")
        assert delivery["status"] == "denied"
        assert not c.db["journal"] and not c.state["posts"]


async def grant(c, delivery):
    return await c.service.authority.configure(
        c.project,
        c.repository,
        SCMActorBindingInputV1(
            version=1,
            request_id=uuid4().hex,
            expected_previous_digest=None,
            expected_project_binding_digest=digest(c.definition),
            verified_receipt_id=delivery["id"],
            expected_verified_reference_digest=digest(delivery["verification"]),
            native_user_id=c.user_id,
            component_ids=["api"],
            allowed_actions=["status"],
            active=True,
            acknowledge_webhook_asserted_identity=True,
        ),
    )


async def test_signed_status_uses_current_native_status_consent_without_an_invented_run(poll_case):
    async with poll_case() as c:
        delivery, comment = await signed_address(c)
        c.state["comments"].append(comment)
        authority = c.service.authority
        consent = await grant(c, delivery)
        scopes = await authority.conversation_scopes(delivery)
        assert len(scopes) == 1 and scopes[0]["action"] == "status"
        assert scopes[0]["binding_digest"] == consent["digest"]
        assert scopes[0]["native_user_id"] == c.user_id and scopes[0]["component_ids"] == ["api"]
        assert "run_id" not in scopes[0]
        assert not c.db["journal"] and not c.state["posts"]
        c.collaboration.polling = None
        result = await c.collaboration.process(delivery["id"])
        assert result["status"] == "processed" and result["run_id"] is None
        assert result["conversation"]["selection"] == "missing"
        assert result["feedback_status"] == "sent" and len(c.state["posts"]) == 1


@pytest.mark.parametrize("change", ["user", "membership", "source", "permission", "actor", "command"])
async def test_signed_conversation_rechecks_current_authority_before_any_reply(poll_case, change):
    async with poll_case() as c:
        delivery, comment = await signed_address(c)
        c.state["comments"].append(comment)
        await grant(c, delivery)
        if change == "user":
            c.db["users"][c.user_id].enabled = False
        elif change == "membership":
            c.db["memberships"]["member"].active = False
        elif change == "source":
            c.state["edited"] = True
        elif change == "permission":
            c.state["external_permission"] = "none"
        elif change == "actor":
            c.db["settings"][c.repository].data["trusted_actors"] = []
        else:
            c.db["settings"][c.repository].data["allowed_commands"] = []
        result = await c.collaboration.process(delivery["id"])
        assert result["status"] == "denied"
        assert not c.db["journal"] and not c.state["posts"]


async def test_signed_status_uses_authenticated_previous_ticket_receipt_for_api_created_run(poll_case):
    async with poll_case() as c:
        original, comment = await signed_address(c, body="/forge status " + json.dumps({"run_id": c.run_id}))
        c.state["comments"].append(comment)
        await grant(c, original)
        previous = await c.collaboration.process(original["id"])
        assert previous["status"] == "processed" and len(c.state["posts"]) == 1
        delivery, comment = await signed_address(c, number=91)
        c.state["comments"].append(comment)
        result = await c.collaboration.process(delivery["id"])
        assert result["status"] == "processed" and result["run_id"] == c.run_id
        assert result["conversation"]["references"][0]["receipt_id"] == original["id"]
        assert len(c.state["posts"]) == 2 and not c.db["journal"]
