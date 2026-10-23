# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Registered PyFly API and real PostgreSQL custody; all AI/SCM responses remain controlled."""

import copy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from scm_polling_native_fixtures import native_poll as native_poll
from test_scm_poll_functional_native import prepare as functional_prepare

from firefly_forge.model_profiles import ModelProfileInput
from firefly_forge.scm_natural_models import NaturalDecision
from firefly_forge.task_routing import TaskRoutingInput


def prepare(case, monkeypatch, *, allowance=True, maximum=3):
    path, body, _ = functional_prepare(case, consent=False)
    router = case.store.task_router
    router.profiles.save(
        "natural",
        ModelProfileInput(
            name="Controlled native communication",
            provider="agentic",
            values={"model": "openai-responses:public-natural", "api_key": "owned-controlled-natural-key"},
        ),
    )
    case.http.portal.call(router.save, TaskRoutingInput(bindings={"product": "functional", "communication": "natural"}))
    review = case.http.get(path + "/review", params={"binding_request_id": body["expected_binding_request_id"]})
    assert review.status_code == 200, review.text
    cut = review.json()
    body.update(expected_routing_digest=cut["routing_digest"], max_model_turns=maximum)
    grant = None
    if allowance:
        response = case.http.post(path, json=body)
        assert response.status_code == 201, response.text
        grant = response.json()
    case.state.update(natural_calls=0, natural_intent="product", natural_order=1000)

    async def classify(message, context, settings):
        assert settings["ASSISTANT_PROVIDER"] == "agentic"
        assert settings["FORGE_AGENTIC_MODEL"] == "openai-responses:public-natural"
        case.state["natural_calls"] += 1
        if case.state.get("natural_invalid"):
            from firefly_forge.scm_natural_runtime import ConfirmedInvalidReply

            if case.state["natural_calls"] == 1:
                raise ConfirmedInvalidReply([{"type": "literal_error", "loc": ["intent"]}])
        return NaturalDecision(intent=case.state["natural_intent"], message="Controlled coordinator reply.")

    monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
    return path, body, grant


def stamp(case, row):
    case.state["natural_order"] += 1
    value = (datetime(2026, 10, 16, tzinfo=UTC) + timedelta(seconds=case.state["natural_order"])).isoformat()
    row.update(created_at=value, updated_at=value)
    return row


def observe(case, text, *, issue=3, author=7):
    number = 500 + len(case.state["comments"])
    row = stamp(case, case.comment(number, text, issue))
    row["user"]["id"] = author
    case.state["comments"].append(row)
    case.state["clock"] += 3600
    for _ in range(3):
        case.http.portal.call(case.service.step, case.repo["id"])
    return next(
        row for row in case.http.portal.call(case.retained)[2] if row["verification"]["resource_id"] == str(number)
    )


def publish_in_fixture(case, child):
    assert child["feedback_status"] == "sent", child
    source = stamp(case, case.comment(int(child["feedback_remote"]["id"]), child["feedback_intent"]["body"]))
    source["user"] = {"id": 900, "login": "owned-forge-bot", "type": "Bot"}
    case.state["comments"].append(source)


def product_child(case, parent):
    rows = case.http.portal.call(case.retained)[2]
    children = [
        row
        for row in rows
        if row["id"] in parent.get("natural_product_effects", {}) and row["command"]["operation"] == "turn"
    ]
    assert len(children) == 1
    return children[0]


def base(case):
    return f"/api/v1/projects/{case.project['id']}/repositories/{case.repo['id']}/scm-polling/natural-messages"


def assert_original(case, original, wal):
    assert case.http.portal.call(case.store.get_run, case.run["id"]) == original
    assert case.http.portal.call(case.journal.entries, case.run["id"]) == wal
    response = case.http.get(f"/api/v1/repositories/{case.repo['id']}/product")
    assert response.status_code == 204 and response.content == b""


def test_native_natural_product_request_reaches_real_architect_and_native_journal_once(native_poll, monkeypatch):
    case = native_poll
    prepare(case, monkeypatch)
    original = case.http.portal.call(case.store.get_run, case.run["id"])
    wal = case.http.portal.call(case.journal.entries, case.run["id"])
    before = set(case.store.task_router.profiles.manifests.glob("*.json"))
    delivery = observe(case, "@forge, build a CRM with contact imports.")
    result = case.process(delivery)
    assert result["natural_turn"]["state"] == "product_completed", result
    child = product_child(case, result)
    assert child["action_result"]["status"] == "completed"
    assert child["action_result"]["proposal_reviewed"] is False
    assert child["command"]["message"] == "build a CRM with contact imports."
    assert child["feedback_status"] == "sent"
    assert "Public functional clarification" in child["feedback_intent"]["body"]
    assert "Controlled coordinator reply" not in child["feedback_intent"]["body"]
    assert case.state["natural_calls"] == case.state["model_calls"] == case.state["knowledge_calls"] == 1
    assert len(set(case.store.task_router.profiles.manifests.glob("*.json")) - before) == 2
    assert case.process(delivery) == result
    assert case.state["natural_calls"] == case.state["model_calls"] == 1
    retained = case.http.get(base(case) + "/" + delivery["id"])
    assert retained.status_code == 200 and retained.json() == result
    assert_original(case, original, wal)


def test_native_unmentioned_late_product_answer_reuses_exact_producer_and_session_after_restart(
    native_poll, monkeypatch
):
    case = native_poll
    prepare(case, monkeypatch)
    first = case.process(observe(case, "@forge, design the warehouse CRM."))
    completed = product_child(case, first)
    publish_in_fixture(case, completed)
    case.state["natural_intent"] = "answer"
    delivery = observe(case, "Only the warehouse owners may approve it.")
    assert delivery["command"]["source_kind"] == "retained_question_reply"
    result = case.process(delivery)
    assert result["natural_turn"]["state"] == "product_completed", result
    resolution = result["natural_turn"]["resolution"]
    assert resolution["conversation_id"] == completed["action_result"]["conversation_id"]
    assert resolution["turn_id"] == completed["action_result"]["turn_id"]
    second = product_child(case, result)
    assert second["command"]["conversation_id"] == resolution["conversation_id"]
    assert second["command"]["message"] == "Only the warehouse owners may approve it."
    assert case.state["natural_calls"] == case.state["model_calls"] == case.state["knowledge_calls"] == 2
    from firefly_forge.scm_natural_tasks import SCMNaturalTasks

    assert case.http.portal.call(SCMNaturalTasks(case.collaboration).process, delivery["id"]) == result
    assert case.state["natural_calls"] == case.state["model_calls"] == 2


def test_native_exhausted_product_allowance_keeps_answer_then_readmits_same_message_under_fresh_grant(
    native_poll, monkeypatch
):
    case = native_poll
    path, body, grant = prepare(case, monkeypatch, maximum=1)
    first = case.process(observe(case, "@forge, design the warehouse CRM."))
    publish_in_fixture(case, product_child(case, first))
    case.state["natural_intent"] = "answer"
    delivery = observe(case, "The warehouse owner makes the final decision.")
    result = case.process(delivery)
    assert result["natural_turn"]["state"] == "product_pending_permission", result
    assert case.state["model_calls"] == 1 and case.state["natural_calls"] == 2
    request = {
        **body,
        "request_id": uuid4().hex,
        "expected_previous_digest": grant["digest"],
        "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    }
    response = case.http.post(path, json=request)
    assert response.status_code == 201, response.text
    cut = case.http.get(base(case) + f"/{delivery['id']}/admission-review")
    assert cut.status_code == 200, cut.text
    reviewed = cut.json()
    admission = {
        "request_id": uuid4().hex,
        "expected_intake_digest": reviewed["intake_digest"],
        "expected_cut_digest": reviewed["digest"],
    }
    response = case.http.post(base(case) + f"/{delivery['id']}/admissions", json=admission)
    assert response.status_code == 201, response.text
    assert response.json()["natural_turn"]["state"] == "product_completed"
    assert case.state["model_calls"] == 2 and case.state["natural_calls"] == 2
    again = case.http.post(base(case) + f"/{delivery['id']}/admissions", json=admission)
    assert again.status_code == 201 and again.json() == response.json()
    assert case.state["model_calls"] == 2 and case.state["natural_calls"] == 2


@pytest.mark.parametrize("changed", ["unknown", "membership"])
def test_native_product_handoff_unknown_or_revoked_never_replays(native_poll, monkeypatch, changed):
    case = native_poll
    prepare(case, monkeypatch)
    case.state["unknown_model"] = changed == "unknown"
    case.state["revoke_during_model"] = changed == "membership"
    delivery = observe(case, "@forge, design the warehouse CRM.")
    result = case.process(delivery)
    assert result["natural_turn"]["state"] == "needs_reconciliation", result
    assert result["feedback_status"] == "held"
    assert case.state["model_calls"] == 1
    case.process(delivery)
    assert case.state["model_calls"] == 1 and case.state["natural_calls"] == 1


def test_native_one_confirmed_classification_repair_uses_one_route_then_one_architect_allowance(
    native_poll, monkeypatch
):
    case = native_poll
    prepare(case, monkeypatch)
    case.state["natural_invalid"] = True
    result = case.process(observe(case, "@forge, design the warehouse CRM."))
    assert result["natural_turn"]["state"] == "product_completed", result
    assert result["natural_turn"]["classifier_requests_reserved"] == 2
    assert case.state["natural_calls"] == 2 and case.state["model_calls"] == 1


def prepare_worker(case, monkeypatch):
    case.ready()
    binding = case.consent()
    linked = case.process(case.observe(action="status"))
    assert linked["status"] == "processed" and linked["feedback_status"] == "sent"
    case.state.update(natural_calls=0, natural_order=1000)
    publish_in_fixture(case, linked)
    router = case.store.task_router
    router.profiles.save(
        "natural",
        ModelProfileInput(
            name="Controlled worker communication",
            provider="agentic",
            values={"model": "openai-responses:public-natural", "api_key": "owned-controlled-natural-key"},
        ),
    )
    case.http.portal.call(router.save, TaskRoutingInput(bindings={"communication": "natural"}))

    async def classify(message, context, settings):
        case.state["natural_calls"] += 1
        assert settings["ASSISTANT_PROVIDER"] == "agentic"
        return NaturalDecision(intent="answer", message="The original answer is retained.")

    monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
    return binding


def test_native_worker_late_answer_and_fresh_resume_keep_same_run_and_durable_response(native_poll, monkeypatch):
    case = native_poll
    binding = prepare_worker(case, monkeypatch)
    before = case.http.portal.call(case.store.get_run, case.run["id"])
    delivery = observe(case, "Keep the existing result.")
    assert delivery["command"]["source_kind"] == "retained_question_reply"
    result = case.process(delivery)
    assert result["natural_turn"]["state"] == "answer_saved_pending_resume", result
    entries = case.http.portal.call(case.journal.entries, case.run["id"])
    responses = [entry for entry in entries if entry["kind"] == "worker_response"]
    assert len(responses) == 1 and responses[0]["payload"]["answer"] == "Keep the existing result."
    assert case.http.portal.call(case.store.get_run, case.run["id"])["status"] == "paused"
    grant = dict(
        version=3,
        transport="github_rest_poll_v1",
        request_id=uuid4().hex,
        expected_previous_digest=None,
        expected_project_binding_digest=binding["project_binding_digest"],
        expected_binding_request_id=binding["request"]["request_id"],
        expected_binding_digest=binding["digest"],
        component_ids=["api"],
        allowed_actions=["resume"],
        active=True,
        acknowledge_same_run_resume=True,
    )
    response = case.http.post(case.path + "/resume-grants", json=grant)
    assert response.status_code == 201, response.text
    cut = case.http.get(base(case) + f"/{delivery['id']}/admission-review")
    assert cut.status_code == 200, cut.text
    response = case.http.post(
        base(case) + f"/{delivery['id']}/admissions",
        json={
            "request_id": uuid4().hex,
            "expected_intake_digest": cut.json()["intake_digest"],
            "expected_cut_digest": cut.json()["digest"],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["natural_turn"]["state"] == "resumed"
    after = case.http.portal.call(case.store.get_run, case.run["id"])
    assert after["id"] == before["id"] and after["status"] == "queued"
    for key in ("repair_budget", "implementation_rounds", "model_selection", "execution_config", "base_revision"):
        assert after.get(key) == before.get(key)
    assert case.state["natural_calls"] == 1
    assert (
        len(
            [
                entry
                for entry in case.http.portal.call(case.journal.entries, case.run["id"])
                if entry["kind"] == "worker_response"
            ]
        )
        == 1
    )


@pytest.mark.parametrize("changed", ["question", "human", "membership"])
def test_native_retained_reply_rechecks_source_and_current_membership_before_classification(
    native_poll, monkeypatch, changed
):
    case = native_poll
    prepare_worker(case, monkeypatch)
    delivery = observe(case, "Keep the existing result.")
    assert delivery["command"]["source_kind"] == "retained_question_reply"
    before = case.http.portal.call(case.journal.entries, case.run["id"])
    if changed == "question":
        target = delivery["command"]["question_refs"][0]["publication_comment_id"]
        next(row for row in case.state["comments"] if str(row["id"]) == target)["body"] += " changed"
    elif changed == "human":
        case.state["comments"][-1]["body"] += " changed"
    else:
        from worker_reply_fixtures import authenticated_operator

        from firefly_forge.project_access import ProjectAccess

        async def revoke():
            async with authenticated_operator(case.store):
                await ProjectAccess(case.store).set_membership(case.project["id"], case.mapped, None)

        case.http.portal.call(revoke)
    result = case.process(delivery)
    assert result["status"] == "denied" and result["action_status"] == "not_dispatched", result
    assert case.state["natural_calls"] == 0
    assert case.http.portal.call(case.journal.entries, case.run["id"]) == before


def test_native_expired_product_model_consent_still_accepts_owner_conversation_answer(native_poll, monkeypatch):
    case = native_poll
    prepare(case, monkeypatch)
    first = case.process(observe(case, "@forge, design the warehouse CRM."))
    published = product_child(case, first)
    publish_in_fixture(case, published)
    case.state["natural_intent"] = "answer"

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=30)

    monkeypatch.setattr("firefly_forge.scm_poll_functional.datetime", Later)
    delivery = observe(case, "The warehouse owner will review it.")
    result = case.process(delivery)
    assert result["natural_turn"]["state"] == "product_pending_permission", result
    assert result["natural_turn"]["resolution"]["conversation_id"] == published["action_result"]["conversation_id"]
    assert result["natural_turn"]["human_message"] == "The warehouse owner will review it."
    assert result["feedback_status"] == "sent"
    assert case.state["natural_calls"] == 2 and case.state["model_calls"] == 1
    assert not result.get("natural_product_effects")
    assert case.process(delivery) == result
    assert case.state["natural_calls"] == 2 and case.state["model_calls"] == 1


def test_native_worker_response_failure_rolls_back_pg_answer_and_never_replays_unknown_intent(native_poll, monkeypatch):
    case = native_poll
    prepare_worker(case, monkeypatch)
    delivery = observe(case, "Keep the existing result.")
    before = case.http.portal.call(case.journal.entries, case.run["id"])
    original = case.journal._append

    async def fail_after_append(*args, **kwargs):
        value = await original(*args, **kwargs)
        if args[3] == "worker_response":
            raise RuntimeError("Owned rollback witness after SQL answer append")
        return value

    monkeypatch.setattr(case.journal, "_append", fail_after_append)
    result = case.process(delivery)
    assert result["natural_turn"]["state"] == "needs_reconciliation", result
    assert case.http.portal.call(case.journal.entries, case.run["id"]) == before
    assert case.http.portal.call(case.store.get_run, case.run["id"])["worker_request"]["status"] == "pending"
    effects = copy.deepcopy(result["natural_effects"])
    assert case.process(delivery)["natural_effects"] == effects
    assert case.state["natural_calls"] == 1


@pytest.mark.parametrize("when", ["during_model", "after_native_completion"])
def test_native_product_late_result_cannot_replace_current_parent_custody(native_poll, monkeypatch, when):
    from firefly_forge.store import identifier

    case = native_poll
    prepare(case, monkeypatch)
    delivery = observe(case, "@forge, design the warehouse CRM.")
    captured = {}

    async def change_parent():
        async with case.store._unit():
            row = await case.collaboration.inbox.find_by_id(identifier(delivery["id"]))
            await case.collaboration._write_row(
                row,
                status="needs_reconciliation",
                action_status="needs_reconciliation",
                natural_turn={**row.data["natural_turn"], "state": "needs_reconciliation"},
                reason="owned_operator_custody_witness",
                feedback_status="held",
            )
            captured["receipt"] = copy.deepcopy(row.data)

    if when == "during_model":
        original = case.collaboration.product_assistant.designer

        async def delayed(*args, **kwargs):
            result = await original(*args, **kwargs)
            await change_parent()
            return result

        monkeypatch.setattr(case.collaboration.product_assistant, "designer", delayed)
    else:
        service = case.collaboration.poll_functional_authority
        original = service.process

        async def delayed(receipt):
            result = await original(receipt)
            if result["command"]["operation"] == "turn":
                await change_parent()
            return result

        monkeypatch.setattr(service, "process", delayed)
    result = case.process(delivery)
    assert result == captured["receipt"]
    assert result["reason"] == "owned_operator_custody_witness" and result["feedback_status"] == "held"
    assert case.state["model_calls"] == 1
    assert case.process(delivery) == result
    assert case.state["model_calls"] == 1 and case.state["natural_calls"] == 1
