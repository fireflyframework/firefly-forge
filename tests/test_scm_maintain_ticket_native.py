# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared native maintain communication; real Store/lease/Git, controlled transports only."""

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from checkpoint_test_native_fixtures import checkpoint_native as checkpoint_native  # noqa: F401
from natural_coordinator_model_fixture import native_coordinator
from scm_polling_native_fixtures import native_poll as native_poll  # noqa: F401
from test_addressed_status_native import natural
from worker_reply_fixtures import authenticated_operator

from firefly_forge.git_delivery_models import GitDeliveryPolicyInputV1
from firefly_forge.git_delivery_policy import GitDeliveryPolicies
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import digest
from firefly_forge.providers import AgentResult
from firefly_forge.scm_conversations import SCMConversations


@pytest.fixture
def maintain_ticket(checkpoint_native, request):
    from native_client_knowledge_fixtures import native_maintain_knowledge_fixture

    with native_maintain_knowledge_fixture(checkpoint_native) as c:
        owner_text = getattr(request, "param", "@forge keep me informed about this maintenance unit")

        async def disable_optional_git():
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

        c.case.http.portal.call(disable_optional_git)
        body = copy.deepcopy(c.body)
        body["input"].update(operation="maintain", brief="Ask one decision about this independent maintenance unit")
        creation_id = uuid4().hex
        response = c.case.http.post("/api/v1/run-creation-requests/" + creation_id, json=body)
        assert response.status_code == 201, response.text
        creation = response.json()
        assert creation["run_id"] != c.parent["id"] and creation["source_checkpoint"]["operation"] == "maintain"
        calls = []

        class Worker:
            async def execute(self, workspace, *args):
                calls.append(workspace)
                assert (workspace / "product.py").read_text() == "value = 2\n"
                return AgentResult(
                    json.dumps(
                        {
                            "summary": "A maintenance decision is required",
                            "feedback": {
                                "id": "maintain-ticket-feedback",
                                "role_id": "implementer",
                                "task_id": "implement",
                                "attempt": 1,
                                "disposition": "clarification",
                                "summary": "Choose the expected maintenance result",
                                "request": {
                                    "id": "maintain-ticket-question",
                                    "kind": "other",
                                    "question": "Keep the existing contacts?",
                                    "context": "A fresh unit keeps the retained source and its records.",
                                    "choices": ["Keep", "Review"],
                                },
                            },
                        }
                    ),
                    "controlled native maintain fixture",
                )

        c.engine.provider_factory = lambda _: Worker()

        async def execute_child():
            c.engine.active[creation["run_id"]] = asyncio.current_task()
            try:
                await c.engine.execute_run(creation["run_id"])
            finally:
                c.engine.active.pop(creation["run_id"], None)
            child = await c.store.get_run(creation["run_id"])
            assert child["status"] == "paused", child.get("error")
            assert child["worker_request"]["id"] == "maintain-ticket-question"
            assert "issue_context" not in child or child["issue_context"] == c.parent.get("issue_context")
            assert len(calls) == 1 and child["implementation_rounds"] == 1
            assert await c.store.get_run(c.parent["id"]) == c.parent
            assert await c.journal.entries(c.parent["id"]) == c.prefix
            await c.store.release_ownership()
            await c.case.store.acquire_ownership()
            return child

        child = c.case.http.portal.call(execute_child)
        c.case.ready()
        c.case.consent()
        c.case.run.clear()
        c.case.run.update(c.parent)
        prior = c.case.process(c.case.observe("status"))
        assert prior["run_id"] == c.parent["id"]
        observed = natural(c.case, owner_text, number=801)
        path = f"/api/v1/projects/{c.case.project['id']}/repositories/{c.case.repo['id']}/maintain-ticket-bindings"
        request = dict(
            version=1,
            request_id=uuid4().hex,
            run_id=child["id"],
            creation_request_id=creation_id,
            expected_creation_digest=creation["digest"],
            observed_receipt_id=observed["id"],
            expected_reference_digest=digest(observed["verification"]),
            expected_previous_digest=None,
            lifetime_seconds=1800,
            max_notifications=8,
            allowed_actions=["status", "question", "answer"],
            acknowledge_native_communication=True,
        )
        yield c, child, creation, observed, path, request


def configure(values):
    c, child, creation, observed, path, request = values
    response = c.case.http.post(path, json=request)
    assert response.status_code == 201, response.text
    kept = response.json()
    assert c.case.http.get(path + "/requests/" + request["request_id"]).json() == kept
    assert c.case.http.post(path, json=request).json() == kept
    return kept


def test_native_bound_maintain_question_owner_answer_retains_pause_and_parent(maintain_ticket, monkeypatch):
    c, child, creation, observed, path, request = maintain_ticket

    async def readonly_before_binding():
        service = SCMConversations(c.case.collaboration)
        scopes = await service.scopes(observed, remote=False)
        refs, _ = await service.references(observed, scopes, durable=True)
        ref = next(ref for ref in refs if ref["run_id"] == child["id"])
        assert ref["kind"] == "native_checkpoint_maintain"

    c.case.http.portal.call(readonly_before_binding)
    kept = configure(maintain_ticket)
    previous = len(c.case.state["posts"])
    c.case.http.portal.call(c.case.collaboration.sync_feedback)
    anchor = c.case.http.portal.call(c.case.collaboration.receipt, kept["outbox_id"])
    assert anchor["feedback_status"] == "sent" and anchor["feedback_intent"]["request_id"] == "maintain-ticket-question"
    assert len(c.case.state["posts"]) >= previous + 1
    assert not anchor.get("native_question") and anchor["maintain_ticket"]["binding_digest"] == kept["digest"]
    c.case.http.portal.call(c.case.collaboration.feedback, anchor["id"])
    assert c.case.http.portal.call(c.case.collaboration.receipt, anchor["id"]) == anchor
    coordinator = native_coordinator(c.case, monkeypatch, intent="answer")

    async def grounded_answer(message, context, settings):
        from pydantic_ai.messages import ModelResponse, ToolCallPart
        from pydantic_ai.models.function import FunctionModel

        from firefly_forge.scm_natural_models import question_choices
        from firefly_forge.scm_natural_runtime import classify

        offered = next(row for row in question_choices(context) if row["run_id"] == child["id"])
        matching = [row for row in context["candidates"] if "existing contacts" in (row.get("question") or "")]
        assert len(matching) == 1 and matching[0]["run_id"] == child["id"]
        assert "existing contacts" in message

        def model(messages, info):
            assert not info.function_tools
            coordinator.append((message, context))
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        {
                            "intent": "answer",
                            "message": "Your maintenance answer is retained.",
                            "choice": offered["choice"],
                            "selection_evidence": {
                                "human_span": "existing contacts",
                                "question_span": "existing contacts",
                            },
                        },
                    )
                ]
            )

        return await classify(message, context, settings, model=FunctionModel(model))

    monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", grounded_answer)
    stamp = datetime.now(UTC)
    # The controlled provider exposes every comment already confirmed by a native
    # POST, including the still-pending parent question; it does not fabricate receipts.
    for index, posted in enumerate(c.case.state["posts"], 1):
        bot = c.case.comment(100 + index, posted["body"])
        bot["user"]["type"] = "Bot"
        posted_at = (stamp + timedelta(milliseconds=index)).isoformat()
        bot.update(created_at=posted_at, updated_at=posted_at)
        c.case.state["comments"].append(bot)
    human = c.case.comment(850, "Keep the existing contacts.")
    human.update(
        created_at=(stamp + timedelta(seconds=1)).isoformat(), updated_at=(stamp + timedelta(seconds=1)).isoformat()
    )
    c.case.state["comments"].append(human)
    c.case.state["clock"] += 3600
    for _ in range(2):
        c.case.http.portal.call(c.case.service.step, c.case.repo["id"])
    reply = next(
        row for row in c.case.http.portal.call(c.case.retained)[2] if row["verification"]["resource_id"] == "850"
    )

    async def retained_reply_projection():
        # Exercise the same retained-reply reader under a physical native unit so a
        # swallowed polling refusal has an actionable traceback before processing.
        async with c.case.store._unit():
            projected = await c.case.service._natural_reply_source(
                c.case.repo["id"], reply["source_snapshot"], reply["verification"], False
            )
            assert projected is not None, {key: reply.get(key) for key in ("status", "reason", "command")}
        return projected

    projection = c.case.http.portal.call(retained_reply_projection)
    assert projection.adapter == "retained_question_reply_v1"
    assert reply["status"] == "received", reply
    assert reply["command"]["adapter"] == "retained_question_reply_v1", reply
    result = c.case.process(reply)
    assert "natural_turn" in result, result
    assert result["natural_turn"]["state"] == "answer_saved_pending_resume", result
    assert len(coordinator) == 1

    async def retained():
        current = await c.store.get_run(child["id"])
        entries = await c.journal.entries(child["id"])
        responses = [entry for entry in entries if entry["kind"] == "worker_response"]
        assert len(responses) == 1 and responses[0]["payload"]["answer"] == "Keep the existing contacts."
        assert responses[0]["payload"]["responding_actor"]["user_id"] == c.case.mapped.id.hex
        assert current["status"] == "paused" and current["implementation_rounds"] == child["implementation_rounds"]
        assert not any(entry["kind"] in {"resume", "provider_dispatch_intent"} for entry in entries)
        assert await c.store.get_run(c.parent["id"]) == c.parent
        assert await c.journal.entries(c.parent["id"]) == c.prefix
        assert await c.case.store.repository(c.case.repo["id"]) == await c.store.repository(c.case.repo["id"])

    c.case.http.portal.call(retained)


def test_native_maintain_owner_revocation_before_provider_dispatch_posts_nothing(maintain_ticket, monkeypatch):
    c, child, _, _, _, _ = maintain_ticket
    kept = configure(maintain_ticket)
    original = c.case.scm._request

    async def request(method, url, **kwargs):
        if method == "POST" and kwargs.get("before_dispatch"):
            async with c.case.store._unit():
                users = ForgeIdentityUserRepository()
                user = await users.find_by_id(c.case.mapped.id)
                user.enabled = False
                await users.save(user)
        return await original(method, url, **kwargs)

    monkeypatch.setattr(c.case.scm, "_request", request)
    before = len(c.case.state["posts"])
    c.case.http.portal.call(c.case.collaboration.feedback, kept["outbox_id"])
    anchor = c.case.http.portal.call(c.case.collaboration.receipt, kept["outbox_id"])
    assert anchor["feedback_status"] == "held" and anchor["reason"] == "native_maintain_authority_changed"
    assert len(c.case.state["posts"]) == before
    assert c.case.http.portal.call(c.store.get_run, child["id"]) == child
    assert c.case.http.portal.call(c.store.get_run, c.parent["id"]) == c.parent


def test_native_maintain_unknown_notification_never_reposts_or_silently_rebinds(maintain_ticket):
    c, child, _, _, path, request = maintain_ticket
    kept = configure(maintain_ticket)
    before = len(c.case.state["posts"])
    c.case.state["lost_reply"] = True
    c.case.http.portal.call(c.case.collaboration.feedback, kept["outbox_id"])
    anchor = c.case.http.portal.call(c.case.collaboration.receipt, kept["outbox_id"])
    assert anchor["feedback_status"] == "needs_reconciliation"
    c.case.http.portal.call(c.case.collaboration.feedback, kept["outbox_id"])
    assert c.case.http.portal.call(c.case.collaboration.receipt, kept["outbox_id"]) == anchor
    assert len(c.case.state["posts"]) == before + 1
    renewal = {**request, "request_id": uuid4().hex, "expected_previous_digest": kept["digest"]}
    denied = c.case.http.post(path, json=renewal)
    assert denied.status_code == 403, denied.text
    assert c.case.http.portal.call(c.store.get_run, child["id"]) == child
    assert c.case.http.portal.call(c.store.get_run, c.parent["id"]) == c.parent


@pytest.mark.parametrize(
    "maintain_ticket",
    ["@forge, por favor informa sobre el trabajo y pregúntame si hace falta una decisión."],
    indirect=True,
    ids=["spanish-owner"],
)
def test_native_contextual_maintain_uses_verified_owner_quotation_without_resuming(maintain_ticket):
    import hashlib

    from communication_model_fixture import reviewed_model
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, UserPromptPart

    c, child, creation, observed, path, request = maintain_ticket
    response = c.case.http.put(
        "/api/v1/model-profiles/contextual-updates",
        json={
            "name": "Controlled contextual updates",
            "provider": "agentic",
            "values": {"model": "openai-responses:fixture", "api_key": "public-fixture-context-model-key"},
        },
    )
    assert response.status_code == 200, response.text
    previous = c.case.http.get("/api/v1/task-routing")
    assert previous.status_code == 200, previous.text
    routing = previous.json()
    response = c.case.http.put(
        "/api/v1/task-routing",
        json={
            "bindings": {**routing["bindings"], "communication": "contextual-updates"},
            "expected_digest": routing["digest"],
        },
    )
    assert response.status_code == 200, response.text
    calls = []

    def draft(messages, info):
        prompt = next(
            part.content
            for msg in reversed(messages)
            for part in msg.parts
            if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        )
        prefix = "Draft the project update using only this recorded fact packet:\n"
        facts, _ = json.JSONDecoder().raw_decode(prompt.removeprefix(prefix))
        references = sorted(facts)
        payload = {
            "title": {"text": "Decisión sobre el trabajo solicitado", "fact_ids": references[:4]},
            "paragraphs": [
                {
                    "text": "La unidad está pausada y necesita una decisión del propietario.",
                    "fact_ids": references[start : start + 4],
                }
                for start in range(4, len(references), 4)
            ]
            or [
                {"text": "La unidad está pausada y necesita una decisión del propietario.", "fact_ids": references[:4]}
            ],
            "decision": {key: facts["decision"][key] for key in ("question", "context", "choices")},
        }
        part = ToolCallPart(info.output_tools[0].name, payload) if info.output_tools else TextPart(json.dumps(payload))
        return ModelResponse(parts=[part])

    c.case.collaboration.communication_writer.model = reviewed_model(draft, calls=calls)
    kept = configure(maintain_ticket)
    c.case.http.portal.call(c.case.collaboration.sync_feedback)
    sent = c.case.http.portal.call(c.case.collaboration.receipt, kept["outbox_id"])
    assert sent["feedback_status"] == "sent", sent.get("reason")
    owner = observed["source_snapshot"]["body"]
    assert owner in str(calls)
    assert (
        sent["feedback_intent"]["communication"]["facts"]["audience"]["source_sha256"]
        == hashlib.sha256(owner.encode()).hexdigest()
    )
    assert "Decisión sobre el trabajo solicitado" in sent["feedback_intent"]["body"]
    assert "Keep the existing contacts?" in sent["feedback_intent"]["body"]
    assert c.case.http.portal.call(c.store.get_run, child["id"])["status"] == "paused"
    assert c.case.http.portal.call(c.store.get_run, c.parent["id"]) == c.parent
    assert c.case.http.portal.call(c.journal.entries, c.parent["id"]) == c.prefix
