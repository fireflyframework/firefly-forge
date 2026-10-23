# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Historical outbox proof stays immutable under a separately current read binding."""
# ruff: noqa: F401, F811

import copy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_communication_writer import configured_store
from test_native_question_publication_host import pending
from test_scm_polling import poll_case

from firefly_forge.native_question_publications import NativeQuestionPublications
from firefly_forge.project_access import AccessDenied
from firefly_forge.scm_natural_effects import NaturalEffects, NaturalReadmission
from firefly_forge.scm_natural_models import NaturalDecision
from firefly_forge.scm_natural_tasks import SCMNaturalTasks
from firefly_forge.scm_polling import STATE_KEY, poll_key


async def published(c, monkeypatch):
    original_consent = c.consent

    async def status_only():
        first = await original_consent()
        request = c.state["consent_request"].model_copy(
            update={
                "request_id": uuid4().hex,
                "expected_previous_digest": first["digest"],
                "allowed_actions": ["status"],
            }
        )
        c.state["consent_request"] = request
        return await c.service.configure_binding(c.project, c.repository, request)

    c.consent = status_only
    original, _ = await pending(c, monkeypatch)
    producer = NativeQuestionPublications(c.collaboration)
    record = await producer.produce(c.run_id)
    await c.collaboration.feedback(record["id"])
    sent = await c.collaboration.receipt(record["id"])
    assert sent["feedback_status"] == "sent"
    return producer, original, sent


async def renew(c):
    before = c.state["consent_request"]
    current = await c.service._head(
        c.project, c.repository, poll_key(c.project, next(iter(c.db["inbox"].values())).data["verification"])
    )
    request = before.model_copy(
        update={
            "request_id": uuid4().hex,
            "expected_previous_digest": current["digest"],
            "allowed_actions": ["status", "respond"],
        }
    )
    c.state["consent_request"] = request
    return await c.service.configure_binding(c.project, c.repository, request)


async def test_published_native_reference_survives_same_owner_read_consent_renewal(poll_case, monkeypatch):
    async with poll_case() as c:
        producer, original, sent = await published(c, monkeypatch)
        await producer.authorize_reference(sent, await c.store.get_run(c.run_id))
        renewed = await renew(c)
        assert renewed["digest"] != sent["native_question"]["binding_digest"]
        await producer.authorize_reference(sent, await c.store.get_run(c.run_id))
        assert await c.collaboration.receipt(sent["id"]) == sent
        assert c.db["inbox"][original["id"]].data == original
        assert len(c.state["posts"]) == 1
        # Historical proof must never authorize a new publication under the replaced activation binding.
        with pytest.raises(AccessDenied):
            await producer.produce(c.run_id)


async def test_same_retained_native_answer_readmits_after_status_to_respond_renewal_without_new_ai(
    poll_case, monkeypatch, tmp_path
):
    async with poll_case() as c:
        producer, original, sent = await published(c, monkeypatch)
        c.store.task_router = (await configured_store(tmp_path / "models")).task_router
        calls = []

        async def classify(message, context, settings):
            calls.append(copy.deepcopy(context))
            assert any(row.get("publication", {}).get("receipt_id") == sent["id"] for row in context["candidates"])
            return NaturalDecision(intent="answer", message="Your answer is retained.")

        monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
        stamp = datetime.now(UTC)
        bot = c.comment(501, sent["feedback_intent"]["body"])
        bot["user"]["type"] = "Bot"
        bot.update(created_at=stamp.isoformat(), updated_at=stamp.isoformat())
        human = c.comment(502, "Use the original scope.")
        human.update(
            created_at=(stamp + timedelta(seconds=1)).isoformat(), updated_at=(stamp + timedelta(seconds=1)).isoformat()
        )
        c.state["comments"].extend([bot, human])
        c.state["clock"] += 3600
        for _ in range(3):
            await c.service.step(c.repository)
        reply = next(r.data for r in c.db["inbox"].values() if r.source_key == "comment:502")
        result = await c.collaboration.process(reply["id"])
        assert result["natural_turn"]["state"] == "answer_pending_permission"
        assert len(calls) == 1
        assert not [e for e in c.db["journal"].values() if e.kind == "worker_response"]
        old_source = copy.deepcopy(result["command"])
        renewed = await renew(c)
        effects = NaturalEffects(SCMNaturalTasks(c.collaboration))
        cut = await effects.review(reply["id"])
        assert cut["respond_allowed"] is True and cut["scopes"][0]["binding_digest"] == renewed["digest"]
        result = await effects.readmit(
            reply["id"],
            NaturalReadmission(
                request_id=uuid4().hex, expected_intake_digest=cut["intake_digest"], expected_cut_digest=cut["digest"]
            ),
        )
        assert result["natural_turn"]["state"] == "answer_saved_pending_resume"
        assert result["command"] == old_source
        responses = [e for e in c.db["journal"].values() if e.kind == "worker_response"]
        assert len(responses) == 1 and responses[0].data["payload"]["answer"] == "Use the original scope."
        assert responses[0].data["payload"]["responding_actor"]["provenance"]["binding_digest"] == renewed["digest"]
        assert len(calls) == 1
        assert c.db["runs"][c.run_id].data["status"] == "paused"
        assert await c.collaboration.receipt(sent["id"]) == sent
        assert c.db["inbox"][original["id"]].data == original
        assert not [e for e in c.db["journal"].values() if e.kind in {"resume", "provider_dispatch_intent"}]


@pytest.mark.parametrize(
    "changed",
    [
        "revoked",
        "foreign_user",
        "components",
        "membership",
        "identity",
        "archived_pointer",
        "publication",
        "outbox_proof",
    ],
)
async def test_renewed_reference_requires_archived_proof_and_current_same_owner_read_scope(
    poll_case, monkeypatch, changed
):
    async with poll_case() as c:
        c.definition["definition"]["components"].append({"id": "web", "repository_id": c.repository})
        producer, original, sent = await published(c, monkeypatch)
        if changed in {"revoked", "foreign_user", "components"}:
            previous = c.state["consent_request"]
            current = await c.service._head(c.project, c.repository, poll_key(c.project, sent["verification"]))
            update = {
                "request_id": uuid4().hex,
                "expected_previous_digest": current["digest"],
                "allowed_actions": ["status", "respond"],
            }
            if changed == "revoked":
                update["active"] = False
            elif changed == "foreign_user":
                membership = copy.deepcopy(c.db["memberships"]["member"])
                membership.user_id = c.owner.id
                c.db["memberships"]["replacement"] = membership
                update["native_user_id"] = c.owner.id.hex
            else:
                update["component_ids"] = ["api", "web"]
            await c.service.configure_binding(c.project, c.repository, previous.model_copy(update=update))
        else:
            await renew(c)
            if changed == "membership":
                c.db["memberships"]["member"].active = False
            elif changed == "identity":
                c.db["users"][c.user_id].subject = "another-native-identity"
            elif changed == "archived_pointer":
                state = c.db["settings"][c.repository].data[STATE_KEY]
                state["requests"] = {
                    k: v
                    for k, v in state["requests"].items()
                    if v["digest"] != sent["native_question"]["binding_digest"]
                }
            elif changed == "publication":
                sent["feedback_intent"]["body"] = "A different question"
            else:
                sent["native_question"]["feedback_digest"] = "9" * 64
        before = {key: copy.deepcopy(row.data) for key, row in c.db["journal"].items()}
        with pytest.raises((AccessDenied, ValueError)):
            await producer.authorize_reference(sent, await c.store.get_run(c.run_id))
        assert {key: row.data for key, row in c.db["journal"].items()} == before
        assert len(c.state["posts"]) == 1
        assert c.db["inbox"][original["id"]].data == original


@pytest.mark.parametrize("waitpoint", ["archive", "membership"])
async def test_head_revocation_during_historical_reference_await_is_refused(poll_case, monkeypatch, waitpoint):
    async with poll_case() as c:
        producer, original, sent = await published(c, monkeypatch)
        renewed = await renew(c)
        changed = False
        before_settings = copy.deepcopy(c.db["settings"][c.repository].data)
        before_events = set(c.db["events"])
        request = c.state["consent_request"].model_copy(
            update={"request_id": uuid4().hex, "expected_previous_digest": renewed["digest"], "active": False}
        )
        await c.service.configure_binding(c.project, c.repository, request)
        committed_settings = copy.deepcopy(c.db["settings"][c.repository].data)
        committed_events = {key: c.db["events"].pop(key) for key in set(c.db["events"]) - before_events}
        c.db["settings"][c.repository].data = before_settings

        async def revoke():
            nonlocal changed
            changed = True
            # A separate connection commits this already validated immutable event during our await.
            c.db["events"].update(committed_events)
            c.db["settings"][c.repository].data = copy.deepcopy(committed_settings)

        if waitpoint == "archive":
            event = c.service._event

            async def event_wait(pointer, *args):
                result = await event(pointer, *args)
                if not changed and pointer["digest"] == sent["native_question"]["binding_digest"]:
                    await revoke()
                return result

            monkeypatch.setattr(c.service, "_event", event_wait)
        else:
            members = c.service.memberships.for_user

            async def member_wait(user):
                result = await members(user)
                if not changed and user.hex == c.user_id:
                    await revoke()
                return result

            monkeypatch.setattr(c.service.memberships, "for_user", member_wait)
        with pytest.raises(AccessDenied):
            await producer.authorize_reference(sent, await c.store.get_run(c.run_id))
        assert changed
        assert len(c.state["posts"]) == 1
        assert c.db["inbox"][original["id"]].data == original
