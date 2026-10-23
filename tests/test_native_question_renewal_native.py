# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native outbox → same retained answer → renewed consent; provider/model are controlled."""
# ruff: noqa: F401, F811

import copy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from scm_polling_native_fixtures import native_poll  # noqa: F401
from test_native_question_publication_native import prepare

from firefly_forge.model_profiles import ModelProfileInput
from firefly_forge.scm_natural_models import NaturalDecision
from firefly_forge.task_routing import TaskRoutingInput


@pytest.mark.parametrize("native_poll", ["implementation"], indirect=True)
@pytest.mark.parametrize("revoked", [False, True])
def test_native_retained_outbox_answer_requires_same_owner_current_renewed_read_consent(
    native_poll, monkeypatch, revoked
):  # noqa: F811
    case = native_poll
    original_consent = case.consent
    bindings = []

    def status_only():
        first = original_consent()
        request = {
            **first["request"],
            "request_id": uuid4().hex,
            "expected_previous_digest": first["digest"],
            "allowed_actions": ["status"],
        }
        response = case.http.post(case.path + "/bindings", json=request)
        assert response.status_code == 201, response.text
        binding = response.json()
        bindings.append(binding)
        return binding

    case.consent = status_only
    env = prepare(case)

    async def publish():
        record = await env.producer.produce(case.run["id"])
        await env.producer.publish(record["id"])
        return await case.collaboration.receipt(record["id"])

    try:
        sent = case.http.portal.call(publish)
        assert sent["feedback_status"] == "sent"
        router = case.store.task_router
        router.profiles.save(
            "retained-answer",
            ModelProfileInput(
                name="Controlled retained-answer communication",
                provider="agentic",
                values={"model": "openai-responses:public-fixture", "api_key": "owned-native-question-fixture-key"},
            ),
        )
        case.http.portal.call(router.save, TaskRoutingInput(bindings={"communication": "retained-answer"}))
        calls = []

        async def classify(message, context, settings):
            calls.append(copy.deepcopy(context))
            assert any(r.get("publication", {}).get("receipt_id") == sent["id"] for r in context["candidates"])
            return NaturalDecision(intent="answer", message="Your original answer is retained.")

        monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
        stamp = datetime.now(UTC)
        bot = case.comment(501, sent["feedback_intent"]["body"])
        bot["user"]["type"] = "Bot"
        bot.update(created_at=stamp.isoformat(), updated_at=stamp.isoformat())
        human = case.comment(502, "Keep it.")
        human.update(
            created_at=(stamp + timedelta(seconds=1)).isoformat(), updated_at=(stamp + timedelta(seconds=1)).isoformat()
        )
        case.state["comments"].extend([bot, human])
        case.state["clock"] += 3600
        for _ in range(2):
            case.http.portal.call(case.service.step, case.repo["id"])
        reply = next(r for r in case.http.portal.call(case.retained)[2] if r["verification"]["resource_id"] == "502")
        retained = case.process(reply)
        assert retained["natural_turn"]["state"] == "answer_pending_permission", retained
        assert len(calls) == 1
        before = case.http.portal.call(case.journal.entries, case.run["id"])
        assert not [e for e in before if e["kind"] == "worker_response"]
        request = {
            **bindings[0]["request"],
            "request_id": uuid4().hex,
            "expected_previous_digest": bindings[0]["digest"],
            "allowed_actions": ["status", "respond"],
            "active": not revoked,
        }
        response = case.http.post(case.path + "/bindings", json=request)
        assert response.status_code == 201, response.text
        renewed = response.json()
        assert renewed["digest"] != bindings[0]["digest"]
        root = case.path + "/natural-messages/" + retained["id"]
        review = case.http.get(root + "/admission-review")
        if revoked:
            assert review.status_code == 403, review.text
            assert case.http.portal.call(case.journal.entries, case.run["id"]) == before
        else:
            assert review.status_code == 200, review.text
            cut = review.json()
            assert cut["respond_allowed"] is True and cut["scopes"][0]["binding_digest"] == renewed["digest"]
            assert cut["resume_grant_digest"] is None and cut["native_resume_cut"] is None
            admission = {
                "request_id": uuid4().hex,
                "expected_intake_digest": cut["intake_digest"],
                "expected_cut_digest": cut["digest"],
            }
            result = case.http.post(root + "/admissions", json=admission)
            assert result.status_code == 201, result.text
            assert result.json()["natural_turn"]["state"] == "answer_saved_pending_resume"
            again = case.http.post(root + "/admissions", json=admission)
            assert again.status_code == 201 and again.json() == result.json()
            after = case.http.portal.call(case.journal.entries, case.run["id"])
            assert after[: len(before)] == before
            added = after[len(before) :]
            answers = [e for e in added if e["kind"] == "worker_response"]
            assert len(answers) == 1 and answers[0]["payload"]["answer"] == "Keep it."
            assert answers[0]["payload"]["responding_actor"]["provenance"]["binding_digest"] == renewed["digest"]
            assert not [e for e in added if e["kind"] in {"resume", "provider_dispatch_intent"}]
        assert len(calls) == 1
        assert case.http.portal.call(case.collaboration.receipt, sent["id"]) == sent
        assert case.http.portal.call(case.collaboration.receipt, env.original["id"]) == env.original
        current = case.http.portal.call(case.store.get_run, case.run["id"])
        assert current["status"] == "paused"
        for key in ("repair_budget", "implementation_rounds", "execution_config", "base_revision"):
            assert current.get(key) == env.run.get(key)
    finally:
        case.http.portal.call(env.client.aclose)
