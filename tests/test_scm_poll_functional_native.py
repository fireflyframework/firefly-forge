# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native DI/SQL assistant lifecycle with controlled HTTP, knowledge, and model effects."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from communication_model_fixture import reviewed_model
from natural_coordinator_model_fixture import native_coordinator
from scm_polling_native_fixtures import native_poll  # noqa: F401

from firefly_forge.model_profiles import ModelProfileInput
from firefly_forge.product_assistant_runtime import FunctionalAssistantReply
from firefly_forge.task_routing import TaskRoutingInput


def observe(case, operation=None, **options):
    body = options.pop("natural", None) or "/forge assistant " + json.dumps(
        dict(operation=operation, project_id=case.project["id"], **options)
    )
    number = 100 + len(case.state["comments"])
    case.state["comments"].append(case.comment(number, body))
    case.state["clock"] += 3600
    for _ in range(2):
        case.http.portal.call(case.service.step, case.repo["id"])
    return next(
        row for row in case.http.portal.call(case.retained)[2] if row["verification"]["resource_id"] == str(number)
    )


def prepare(case, *, consent=True):
    response = case.http.put(
        f"/api/v1/repositories/{case.repo['id']}/collaboration",
        json=dict(
            enabled=True,
            trusted_actors=["7"],
            configuration_actors=["7"],
            channel_commands=["respond", "assistant"],
            allowed_commands=["status"],
            assistant_agent="bridge",
        ),
    )
    assert response.status_code == 200, response.text
    router = case.store.task_router
    router.profiles.save(
        "functional",
        ModelProfileInput(
            name="Public controlled dialogue",
            provider="bridge",
            values={
                "model": "public-functional",
                "endpoint": "http://127.0.0.1:9",
                "token": "controlled-functional-key",
            },
        ),
    )
    case.http.portal.call(router.save, TaskRoutingInput(bindings={"product": "functional"}))

    evidence = "Public controlled functional knowledge evidence"
    knowledge = case.collaboration.product_assistant.knowledge

    async def retrieve(run):
        assert run["repository_id"] == case.repo["id"]
        assert run["knowledge_snapshot"]["enabled"] is True
        assert knowledge.settings(run)["required"] is True
        case.state["knowledge_calls"] = case.state.get("knowledge_calls", 0) + 1
        return dict(hits=[], warnings=[], citations=[], context=evidence)

    knowledge.retrieve = retrieve

    async def designer(prompt, settings):
        assert case.state["knowledge_calls"] == case.state.get("model_calls", 0) + 1
        assert evidence in prompt
        case.state["model_calls"] = case.state.get("model_calls", 0) + 1
        assert settings["ASSISTANT_OUTPUT_CONTRACT"] == "functional_intent_v1"
        if case.state.get("revoke_during_model"):
            from worker_reply_fixtures import authenticated_operator

            from firefly_forge.project_access import ProjectAccess

            async with authenticated_operator(case.store):
                await ProjectAccess(case.store).set_membership(case.project["id"], case.mapped, "viewer")
        if case.state.get("unknown_model"):
            raise RuntimeError("Controlled unknown provider outcome")
        return FunctionalAssistantReply(
            intent="dialogue", message="Public functional clarification", questions=["Who owns this journey?"]
        )

    case.collaboration.product_assistant.designer = designer
    case.ready()
    binding = case.consent()
    path = case.path + "/functional-grants"
    review = case.http.get(path + "/review", params={"binding_request_id": binding["request"]["request_id"]})
    assert review.status_code == 200, review.text
    cut = review.json()
    body = dict(
        version=1,
        transport="github_rest_poll_v1",
        request_id=uuid4().hex,
        expected_previous_digest=None,
        expected_project_binding_digest=cut["project_binding_digest"],
        expected_binding_request_id=binding["request"]["request_id"],
        expected_binding_digest=binding["digest"],
        component_ids=["api"],
        issue_number=3,
        allowed_actions=["assistant_start", "assistant_status", "assistant_turn"],
        max_model_turns=3,
        expires_at=(datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        model_profile_id=cut["model_profile_id"],
        expected_model_profile_revision=cut["model_profile_revision"],
        expected_model_profile_digest=cut["model_profile_digest"],
        expected_routing_digest=cut["routing_digest"],
        expected_functional_policy_digest=cut["functional_policy_digest"],
        active=True,
        acknowledge_separate_assistant_allowance=True,
    )
    grant = None
    if consent:
        response = case.http.post(path, json=body)
        assert response.status_code == 201, response.text
        grant = response.json()
        assert case.http.get(path + "/requests/" + body["request_id"]).json() == grant
    return path, body, grant


def test_native_old_binding_is_not_functional_model_consent(native_poll):  # noqa: F811
    case = native_poll
    prepare(case, consent=False)
    before = case.http.portal.call(case.store.get_run, case.run["id"])
    result = case.process(observe(case, "start"))
    assert result["status"] == "denied"
    assert case.state.get("model_calls", 0) == 0
    assert case.http.portal.call(case.store.get_run, case.run["id"]) == before


def test_native_functional_issue_chain_reuses_real_assistant_and_preserves_original_run(native_poll, monkeypatch):  # noqa: F811
    case = native_poll
    path, body, _ = prepare(case, consent=False)
    coordinator_calls = native_coordinator(case, monkeypatch, intent="product")
    cut = case.http.get(path + "/review", params={"binding_request_id": body["expected_binding_request_id"]}).json()
    body["expected_routing_digest"] = cut["routing_digest"]
    admitted = case.http.post(path, json=body)
    assert admitted.status_code == 201, admitted.text
    case.collaboration.communication_writer.model = reviewed_model(
        {
            "title": {"text": "Product conversation", "fact_ids": ["assistant"]},
            "paragraphs": [{"text": "The product conversation is ready.", "fact_ids": ["assistant"]}],
            "decision": None,
        }
    )
    before = case.http.portal.call(case.store.get_run, case.run["id"])
    wal = case.http.portal.call(case.journal.entries, case.run["id"])
    started = case.process(observe(case, "start"))
    assert started["status"] == "processed", started
    conversation = started["action_result"]["conversation_id"]
    assert case.state.get("model_calls", 0) == 0
    for index in range(3):
        delivery = observe(case, natural=f"@forge, Una frase humana sin palabras clave. Detalle {index}")
        result = case.process(delivery)
        assert result["status"] == "processed", result
        assert result["natural_turn"]["state"] == "product_completed", result
        children = [
            row for row in case.http.portal.call(case.retained)[2] if row["id"] in result["natural_product_effects"]
        ]
        assert len(children) == 1 and children[0]["command"]["operation"] == "turn"
        assert children[0]["action_result"]["intent"] == "dialogue"
        assert children[0]["action_result"]["proposal_reviewed"] is False
        assert children[0]["action_result"]["conversation_id"] == conversation
        case.process(delivery)
    fourth = case.process(observe(case, "turn", conversation_id=conversation, request_id=uuid4().hex, message="Fourth"))
    assert fourth["status"] == "denied"
    assert case.state["model_calls"] == 3 and len(coordinator_calls) == 3
    assert case.http.portal.call(case.store.get_run, case.run["id"]) == before
    assert case.http.portal.call(case.journal.entries, case.run["id"]) == wal
    product = case.http.get(f"/api/v1/repositories/{case.repo['id']}/product")
    assert product.status_code == 204 and product.content == b""
    assert case.http.portal.call(case.collaboration.product, case.repo["id"]) is None


@pytest.mark.parametrize("changed", ["grant", "source", "profile", "unknown", "membership_during_model"])
def test_native_functional_dispatch_and_unknown_result_fences(native_poll, changed):  # noqa: F811
    case = native_poll
    path, body, grant = prepare(case)
    started = case.process(observe(case, "start"))
    conversation = started["action_result"]["conversation_id"]
    delivery = observe(case, "turn", conversation_id=conversation, request_id=uuid4().hex, message="Public question")
    if changed == "grant":
        response = case.http.post(
            path, json={**body, "request_id": uuid4().hex, "expected_previous_digest": grant["digest"], "active": False}
        )
        assert response.status_code == 201, response.text
    elif changed == "source":
        case.state["comments"][-1]["body"] += " changed"
    elif changed == "profile":
        case.store.task_router.profiles.save(
            "functional",
            ModelProfileInput(
                name="Changed",
                provider="bridge",
                values={
                    "model": "changed-functional",
                    "endpoint": "http://127.0.0.1:9",
                    "token": "controlled-functional-key",
                },
            ),
        )
    elif changed == "membership_during_model":
        case.state["revoke_during_model"] = True
    else:
        case.state["unknown_model"] = True
    result = case.process(delivery)
    if changed in {"unknown", "membership_during_model"}:
        assert result["status"] == "needs_reconciliation"
        assert result["action_status"] == "needs_reconciliation"
        assert result.get("feedback_status") != "pending"
        case.process(delivery)
        assert case.state["model_calls"] == 1
    else:
        assert result["status"] == "denied"
        assert case.state.get("model_calls", 0) == 0


@pytest.mark.parametrize("returned", ["completed", "unconfirmed", "exception"])
def test_native_operator_reconciliation_retains_custody_after_late_model(native_poll, returned):  # noqa: F811
    from worker_reply_fixtures import authenticated_operator

    case = native_poll
    prepare(case)
    started = case.process(observe(case, "start"))
    delivery = observe(
        case,
        "turn",
        conversation_id=started["action_result"]["conversation_id"],
        request_id=uuid4().hex,
        message="Public late outcome",
    )
    original = case.collaboration.product_assistant.turn
    captured = {}

    async def delayed(*args, **kwargs):
        result = await original(*args, **kwargs)
        async with authenticated_operator(case.store):
            captured["value"] = await case.collaboration.reconcile(
                delivery["id"], "not_completed", "Public fixture operator checked the outcome"
            )
        if returned == "exception":
            raise RuntimeError("Controlled late native provider failure")
        if returned == "unconfirmed":
            return {**result, "status": "needs_reconciliation"}
        return result

    case.collaboration.product_assistant.turn = delayed
    assert case.process(delivery) == captured["value"]
    assert case.process(delivery) == captured["value"]
    assert case.state["model_calls"] == 1
