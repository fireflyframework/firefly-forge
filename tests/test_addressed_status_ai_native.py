# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native DI/PostgreSQL bilingual delivery with controlled structured AI, no external model."""

import hashlib
import hmac
import json
from uuid import uuid4

import pytest
from communication_model_fixture import communication_phase, reviewed_model
from natural_coordinator_model_fixture import native_coordinator
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from scm_polling_native_fixtures import native_poll as native_poll
from test_addressed_status_native import natural
from worker_reply_fixtures import authenticated_operator

from firefly_forge.collaboration import CollaborationSettings
from firefly_forge.communication import CommunicationCard, CommunicationConversationV1, communication_facts
from firefly_forge.journal import digest
from firefly_forge.model_profiles import ModelProfileInput
from firefly_forge.scm_actor_authority import SCMActorAuthority
from firefly_forge.scm_authority_models import SCMActorBindingInputV1
from firefly_forge.task_routing import TaskRoutingInput


def signed_natural(c, body):
    comment = c.comment(90, body)
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

    c.http.portal.call(consent)
    return delivery


def configure_ai(c):
    router = c.store.task_router
    router.profiles.save(
        "controlled-updates",
        ModelProfileInput(
            name="Controlled updates",
            provider="agentic",
            values={"model": "openai-responses:fixture", "api_key": "public-native-language-fixture-key"},
        ),
    )
    c.http.portal.call(router.save, TaskRoutingInput(bindings={"communication": "controlled-updates"}))


def card(c, language):
    run = c.http.portal.call(c.store.get_run, c.run["id"])
    facts = communication_facts(
        run, conversation=CommunicationConversationV1(selection="run", language=language, linked_runs=1)
    )
    decision = {key: facts["decision"][key] for key in ("question", "context", "choices")}
    value = {
        "title": {"text": "Estado del trabajo" if language == "es" else "Work status", "fact_ids": ["conversation"]},
        "paragraphs": [
            {
                "text": "Hace falta tu respuesta para continuar."
                if language == "es"
                else "Your answer is needed before work can continue.",
                "fact_ids": ["conversation", "state", "decision"],
            }
        ],
        "decision": decision,
    }

    references = sorted(set(facts) - {"conversation", "state", "decision"})
    for start in range(0, len(references), 4):
        value["paragraphs"].append(
            {
                "text": "La ejecución registra el alcance y el estado de pruebas; la entrega no está acreditada."
                if language == "es"
                else "The execution records its scope and check status; delivery is not established.",
                "fact_ids": references[start : start + 4],
            }
        )
    return value


@pytest.mark.parametrize("transport", ["polling", "signed"])
@pytest.mark.parametrize("language", ["en", "es"])
def test_native_configured_bilingual_card_keeps_actual_ticket_authority_and_replay_inert(
    native_poll, monkeypatch, transport, language
):
    c = native_poll
    c.ready()
    c.consent()
    prior = c.process(c.observe("status"))
    before = c.http.portal.call(c.journal.entries, c.run["id"])
    baseline = len(c.state["posts"])
    body = "@forge ¿cómo va?" if language == "es" else "@forge how is this going?"
    delivery = natural(c, body) if transport == "polling" else signed_natural(c, body)
    configure_ai(c)
    coordinator_calls = native_coordinator(c, monkeypatch) if transport == "polling" else []
    calls = []
    c.collaboration.communication_writer.model = reviewed_model(card(c, language), calls=calls)
    result = c.process(delivery)
    assert result["run_id"] == c.run["id"] and result["conversation"]["selection"] == "run"
    assert result["conversation"]["references"][0]["receipt_id"] == prior["id"]
    assert result["feedback_status"] == "sent" and len(c.state["posts"]) == baseline + 1
    intent = result["feedback_intent"]
    evidence = intent["communication"]
    from firefly_forge.product_provenance import content_digest

    question = body.removeprefix("@forge ") if transport == "polling" else body
    current_facts = communication_facts(
        c.http.portal.call(c.store.get_run, c.run["id"]),
        conversation=CommunicationConversationV1(
            selection="run",
            language=language,
            linked_runs=1 if transport == "polling" else len(result["conversation"]["candidate_ids"]),
            question=question,
            question_sha256=hashlib.sha256(question.encode()).hexdigest(),
        ),
    )
    assert evidence["facts"] == current_facts
    assert evidence["facts_sha256"] == content_digest(current_facts)
    assert evidence["mode"] == "ai"
    assert evidence["review"]["verdict"] == "supported" and not evidence["repair_attempted"]
    assert [call[0] for call in calls] == ["draft", "review"]
    for _, messages, _ in calls:
        prompt = "\n".join(
            part.content
            for message in messages
            for part in message.parts
            if isinstance(getattr(part, "content", None), str)
        )
        assert json.dumps(question, ensure_ascii=False) in prompt
        assert hashlib.sha256(question.encode()).hexdigest() in prompt
    assert all(("Spanish" if language == "es" else "American English") in repr(call[1]) for call in calls)
    if transport == "polling":
        assert evidence["natural_reply_source"] == "current_execution_facts"
        assert len(coordinator_calls) == 1
        assert result["natural_turn"]["classifier_outcome"] == "confirmed_valid"
        assert result["natural_turn"]["decision"]["language"] == language
        assert result["natural_turn"]["human_message"] == question
        assert result["natural_turn"]["reply"] not in intent["body"]
    assert (
        result["source_snapshot"] == delivery["source_snapshot"] and result["verification"] == delivery["verification"]
    )
    assert intent["body_sha256"] == hashlib.sha256(c.state["posts"][-1]["body"].encode()).hexdigest()
    assert c.http.portal.call(c.journal.entries, c.run["id"]) == before
    assert c.process(delivery) == result
    c.http.portal.call(c.collaboration.sync_feedback)
    assert len(calls) == 2 and len(c.state["posts"]) == baseline + 1
    assert len(coordinator_calls) == (0 if transport == "signed" else 1)


@pytest.mark.parametrize("language", ["en", "es"])
def test_native_signed_configured_current_fact_change_holds_without_template_post_or_model_repeat(
    native_poll, language
):
    c = native_poll
    c.ready()
    c.consent()
    c.process(c.observe("status"))
    before = c.http.portal.call(c.journal.entries, c.run["id"])
    baseline = len(c.state["posts"])
    delivery = signed_natural(c, "@forge ¿cómo va?" if language == "es" else "@forge how is this going?")
    draft = card(c, language)
    calls = []
    configure_ai(c)

    async def model(messages, info):
        phase = communication_phase(info)
        calls.append(phase)
        if phase == "draft":
            await c.store.update(c.run["id"], status="failed", worker_request={})
            payload = draft
        else:
            payload = {
                "reader_checks": {
                    "answers_question": True,
                    "relevant_detail": True,
                    "no_redundancy": True,
                    "plain_language": True,
                    "useful_excerpts": True,
                },
                "verdict": "supported",
                "issues": [],
                "fact_ids": sorted(
                    {key for part in [draft["title"], *draft["paragraphs"]] for key in part["fact_ids"]}
                ),
                "rationale": "The original candidate follows its original recorded facts.",
            }
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, payload)])

    c.collaboration.communication_writer.model = FunctionModel(model)
    result = c.process(delivery)
    assert result["feedback_status"] == "held" and result["reason"] == "ai_communication_state_changed"
    assert result["feedback_intent"]["body"] == "" and len(c.state["posts"]) == baseline
    assert calls == ["draft", "review"] and result["feedback_intent"]["communication"]["reason"] == "state_changed"
    c.process(delivery)
    c.http.portal.call(c.collaboration.sync_feedback)
    assert calls == ["draft", "review"] and len(c.state["posts"]) == baseline
    assert c.http.portal.call(c.journal.entries, c.run["id"]) == before


@pytest.mark.parametrize("language,verdict", [("en", "unsupported"), ("es", "uncertain")])
def test_native_signed_one_semantic_correction_is_published_once_without_second_veto(native_poll, language, verdict):
    from firefly_forge.product_provenance import content_digest

    c = native_poll
    c.ready()
    c.consent()
    c.process(c.observe("status"))
    before = c.http.portal.call(c.journal.entries, c.run["id"])
    baseline = len(c.state["posts"])
    delivery = signed_natural(c, "@forge ¿cómo va?" if language == "es" else "@forge how is this going?")
    corrected = card(c, language)
    candidate = {
        **corrected,
        "paragraphs": [
            {
                "text": "El trabajo ha terminado." if language == "es" else "All work has finished.",
                "fact_ids": ["conversation", "state", "decision"],
            },
            *corrected["paragraphs"][1:],
        ],
    }
    configure_ai(c)
    calls = []
    c.collaboration.communication_writer.model = reviewed_model(
        candidate, verdict=verdict, correction=corrected, calls=calls
    )
    result = c.process(delivery)
    intent = result["feedback_intent"]
    evidence = intent["communication"]
    assert result["feedback_status"] == "sent" and evidence["mode"] == "ai"
    assert evidence["repair_attempted"] is True and evidence["review"]["verdict"] == verdict
    assert evidence["review_applies_to"] == "original_candidate"
    assert evidence["review_candidate_sha256"] == content_digest(
        CommunicationCard.model_validate(candidate).model_dump()
    )
    assert (
        evidence["candidate_sha256"]
        == content_digest(CommunicationCard.model_validate(corrected).model_dump())
        != evidence["review_candidate_sha256"]
    )
    assert [call[0] for call in calls] == ["draft", "review", "draft"]
    assert corrected["paragraphs"][0]["text"] in intent["body"]
    assert candidate["paragraphs"][0]["text"] not in intent["body"]
    assert len(c.state["posts"]) == baseline + 1 and c.state["posts"][-1]["body"] == intent["body"]
    assert intent["body_sha256"] == hashlib.sha256(intent["body"].encode()).hexdigest()
    assert c.http.portal.call(c.journal.entries, c.run["id"]) == before
    c.process(delivery)
    c.http.portal.call(c.collaboration.sync_feedback)
    assert [call[0] for call in calls] == ["draft", "review", "draft"] and len(c.state["posts"]) == baseline + 1


def test_native_polling_unknown_coordinator_outcome_retains_intent_without_writer_or_replay(native_poll, monkeypatch):
    c = native_poll
    c.ready()
    c.consent()
    calls = native_coordinator(c, monkeypatch)
    before = c.http.portal.call(c.journal.entries, c.run["id"])
    delivery = natural(c, "@forge how is this going?")
    c.state["coordinator_unknown"] = True
    result = c.process(delivery)
    assert result["status"] == "needs_reconciliation"
    assert result["natural_turn"]["classifier_outcome"] == "unknown"
    assert result["natural_turn"]["reply"] is None and result["feedback_status"] == "held"
    assert len(calls) == 1 and not c.state["posts"]
    assert c.process(delivery) == result
    c.http.portal.call(c.collaboration.sync_feedback)
    assert len(calls) == 1 and not c.state["posts"]
    assert c.http.portal.call(c.journal.entries, c.run["id"]) == before
