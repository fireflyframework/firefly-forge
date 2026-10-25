# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Durable natural intake with current native read authority and controlled AI only."""

import copy
import importlib
import importlib.util
from types import SimpleNamespace

import pytest
from test_scm_polling import poll_case as poll_case

from firefly_forge.scm_natural_models import NaturalDecision


async def prepared(c, monkeypatch, *, intent="answer"):
    assert importlib.util.find_spec("firefly_forge.scm_natural_tasks"), "Durable natural intake is missing"
    module = importlib.import_module("firefly_forge.scm_natural_tasks")
    from datetime import UTC, datetime, timedelta

    original_comment = c.comment

    def timed_comment(number, body):
        value = original_comment(number, body)
        stamp = (datetime(2026, 10, 15, 10, tzinfo=UTC) + timedelta(seconds=number)).isoformat()
        value.update(created_at=stamp, updated_at=stamp)
        return value

    c.comment = timed_comment
    await c.ready()
    await c.consent()
    first = await c.new_command(action="status")
    await c.collaboration.process(first["id"])
    from firefly_forge.model_profiles import ModelProfileInput, ModelProfiles
    from firefly_forge.task_routing import TaskRouting, TaskRoutingInput

    profiles = ModelProfiles(c.store.configuration)
    profiles.save(
        "natural",
        ModelProfileInput(
            name="Public natural route",
            provider="agentic",
            values={"model": "openai-responses:public-fixture", "api_key": "owned-controlled-natural-fixture-key"},
        ),
    )
    c.store.task_router = TaskRouting(profiles)
    await c.store.task_router.save(TaskRoutingInput(bindings={"communication": "natural"}))
    c.state["natural_calls"] = 0
    if intent == "status":
        import json

        from communication_model_fixture import reviewed_model
        from pydantic_ai.messages import ModelResponse, ToolCallPart

        def current_card(messages, info):
            from firefly_forge.communication import CommunicationConversationV1, communication_facts

            facts = communication_facts(
                c.db["runs"][c.run_id].data,
                conversation=CommunicationConversationV1(selection="run", language="en", linked_runs=1),
            )
            keys = list(facts)
            decision = facts.get("decision")
            card = {
                "title": {"text": "Current execution", "fact_ids": ["state"]},
                "paragraphs": [
                    {"text": "Controlled current execution facts.", "fact_ids": keys[index : index + 4]}
                    for index in range(0, len(keys), 4)
                ],
                "decision": {key: decision[key] for key in ("question", "context", "choices")} if decision else None,
            }
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, json.dumps(card))])

        c.collaboration.communication_writer.model = reviewed_model(current_card)

    async def classify(message, context, settings):
        c.state["natural_calls"] += 1
        hook = c.state.get("natural_hook")
        if hook:
            hook()
        if c.state.get("natural_unknown"):
            raise RuntimeError("Controlled unknown classification")
        return NaturalDecision(intent=intent, message="I retained your answer.")

    monkeypatch.setattr(module, "classify", classify)
    c.state["clock"] += 3600
    c.state["comments"].append(c.comment(99, "@forge, sí, conserva los contactos existentes"))
    for _ in range(3):
        await c.service.step(c.repository)
    row = next(r for r in c.db["inbox"].values() if r.source_key == "comment:99")
    return module.SCMNaturalTasks(c.collaboration), row.data["id"]


async def test_late_natural_answer_is_durable_without_any_functional_or_execution_consent(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        # Read consent remains current; lack of respond permission cannot discard the person's words.
        c.db["settings"][c.repository].data["channel_commands"] = []
        result = await service.process(receipt)
        assert result["natural_turn"]["human_message"] == "sí, conserva los contactos existentes"
        assert result["natural_turn"]["decision"]["intent"] == "answer"
        assert result["natural_turn"]["state"] in {"waiting", "answer_pending_permission"}
        assert c.state["natural_calls"] == 1
        assert not c.db["journal"]
        restarted = type(service)(c.collaboration)
        assert await restarted.process(receipt) == result
        assert c.state["natural_calls"] == 1


async def test_unknown_classifier_result_remains_held_across_restart_without_replay(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        c.state["natural_unknown"] = True
        result = await service.process(receipt)
        assert result["natural_turn"]["state"] == "needs_reconciliation"
        assert result["status"] == "needs_reconciliation"
        assert await type(service)(c.collaboration).process(receipt) == result
        assert c.state["natural_calls"] == 1 and not c.db["journal"]


async def test_membership_or_source_change_during_classifier_cannot_apply_an_answer(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        c.state["natural_hook"] = lambda: setattr(c.db["memberships"]["member"], "active", False)
        result = await service.process(receipt)
        assert result["natural_turn"]["state"] == "needs_reconciliation"
        assert c.state["natural_calls"] == 1 and not c.db["journal"]


async def test_communication_route_is_required_without_provider_fallback(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        c.store.task_router = None
        result = await service.process(receipt)
        assert result["natural_turn"]["state"] == "waiting_configuration"
        assert c.state["natural_calls"] == 0 and not c.db["journal"]


async def test_one_confirmed_invalid_reply_reserves_one_correction_and_then_responds(poll_case, monkeypatch):  # noqa: F811
    from firefly_forge.scm_natural_runtime import ConfirmedInvalidReply

    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        calls = []

        async def classify(message, context, settings):
            calls.append(context)
            if len(calls) == 1:
                raise ConfirmedInvalidReply([{"type": "literal_error", "loc": ["intent"]}])
            assert context["repair_contract_errors"] == [{"type": "literal_error", "loc": ["intent"]}]
            return NaturalDecision(intent="dialogue", message="A real corrected reply.")

        monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
        result = await service.process(receipt)
        assert result["status"] == "processed"
        assert result["natural_turn"]["reply"] == "A real corrected reply."
        assert result["natural_turn"]["classifier_requests_reserved"] == 2 and len(calls) == 2
        assert await type(service)(c.collaboration).process(receipt) == result
        assert len(calls) == 2


async def test_second_invalid_reply_is_held_without_a_third_call_or_template_answer(poll_case, monkeypatch):  # noqa: F811
    from firefly_forge.scm_natural_runtime import ConfirmedInvalidReply

    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        calls = []

        async def classify(message, context, settings):
            calls.append(context)
            raise ConfirmedInvalidReply([{"type": "literal_error", "loc": ["intent"]}])

        monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
        result = await service.process(receipt)
        assert result["status"] == "needs_reconciliation"
        assert result["natural_turn"]["reply"] is None and len(calls) == 2
        assert result["natural_turn"]["classifier_requests_reserved"] == 2
        assert result["feedback_status"] == "held"


async def product_context(c, service, receipt):
    from uuid import UUID

    from test_scm_natural_product_questions_host import fixture

    from firefly_forge.communication import assistant_communication, communication_facts

    publication, session, turn, events = fixture()
    original = await c.collaboration.receipt(receipt)
    scopes = await service.conversations.scopes(original, remote=False)
    session.update(repository_id=c.repository, project_id=scopes[0]["project_id"])
    publication.update(repository_id=c.repository, actor=original["actor"], issue=original["issue"])
    publication["functional_authority"] = {"binding_digest": scopes[0]["binding_digest"]}
    publication["verification"] = copy.deepcopy(original["verification"])
    publication["verification"]["resource_id"] = "90"
    publication["feedback_intent"]["communication"]["facts"] = communication_facts(
        None, assistant=assistant_communication(publication)
    )

    async def session_get(key):
        return SimpleNamespace(id=UUID(session["id"]), repository_id=UUID(c.repository), data=copy.deepcopy(session))

    async def turn_get(key):
        return SimpleNamespace(
            id=UUID(turn["id"]), session_id=UUID(session["id"]), status="completed", data=copy.deepcopy(turn)
        )

    async def events_get(key):
        return [SimpleNamespace(data=copy.deepcopy(event)) for event in events]

    c.collaboration.product_assistant = SimpleNamespace(
        records=SimpleNamespace(find_by_id=session_get),
        turns=SimpleNamespace(find_by_id=turn_get),
        events=SimpleNamespace(events=events_get),
    )
    public_source = c.comment(100, publication["feedback_intent"]["body"])
    public_source["user"]["type"] = "Bot"
    c.state["comments"].append(public_source)
    old_context = c.collaboration.inbox.conversation_context

    async def context(delivery):
        return [SimpleNamespace(data=copy.deepcopy(publication), sequence=999), *await old_context(delivery)]

    c.collaboration.inbox.conversation_context = context
    return publication, session, turn, events


async def test_product_answer_binds_typed_completed_turn_and_survives_restart_without_execution(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        publication, session, turn, _ = await product_context(c, service, receipt)
        result = await service.process(receipt)
        retained = result["natural_turn"]
        assert retained["resolution"]["state"] == "product_answer"
        assert retained["resolution"]["conversation_id"] == session["id"]
        assert retained["resolution"]["turn_id"] == turn["id"]
        assert retained["resolution"]["answer"] == "sí, conserva los contactos existentes"
        assert retained["context"]["candidates"][0]["publication"]["comment_id"] == str(
            publication["feedback_remote"]["id"]
        )
        assert result["run_id"] is None and not c.db["journal"]
        assert await type(service)(c.collaboration).process(receipt) == result
        assert c.state["natural_calls"] == 1


async def test_product_question_changed_during_classification_cannot_consume_answer(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        _, _, turn, _ = await product_context(c, service, receipt)
        c.state["natural_hook"] = lambda: turn["reply"]["questions"].append("Injected changed question")
        result = await service.process(receipt)
        assert result["status"] == "needs_reconciliation"
        assert result["natural_turn"]["reply"] is None and not c.db["journal"]


async def test_unmentioned_owner_reply_to_confirmed_product_questions_enters_the_durable_natural_path(
    poll_case, monkeypatch
):  # noqa: F811
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        await product_context(c, service, receipt)
        c.state["clock"] += 3600
        c.state["comments"].append(
            c.comment(101, "Ventas revisa. Operaciones reasigna. [Adjunto](https://example.test/info.png)")
        )
        for _ in range(3):
            await c.service.step(c.repository)
        row = next(row for row in c.db["inbox"].values() if row.source_key == "comment:101")
        assert row.status == "received", row.data.get("reason")
        assert row.data["command"]["source_kind"] == "retained_question_reply"
        result = await c.collaboration.process(row.data["id"])
        assert result["natural_turn"]["resolution"]["state"] == "product_answer"
        assert result["natural_turn"]["human_message"].startswith("Ventas revisa.")
        assert result["feedback_status"] == "sent", result.get("reason")
        assert result["feedback_stage"] == "action_completed"
        assert result["run_id"] is None and not c.db["journal"]


async def test_newly_published_typed_natural_questions_accept_late_unmentioned_answer_once(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch, intent="dialogue")

        async def classify(message, context, settings):
            c.state["natural_calls"] += 1
            if c.state["natural_calls"] == 1:
                return NaturalDecision(
                    intent="dialogue",
                    message="A product discussion.",
                    questions=["Who reviews?", "Who may reassign?"],
                    language="en",
                )
            return NaturalDecision(
                intent="answer", message="Thank you; your product answer is retained.", language="en"
            )

        monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
        first = await c.collaboration.process(receipt)
        assert first["feedback_status"] == "sent"
        assert (
            "Who reviews" in first["feedback_intent"]["body"] and "Who may reassign" in first["feedback_intent"]["body"]
        )
        assert first["feedback_intent"].get("natural_turn_digest"), (
            "The published question producer lacks a retained digest"
        )
        bot = c.comment(101, first["feedback_intent"]["body"])
        bot["user"]["type"] = "Bot"
        c.state["comments"].append(bot)
        c.state["clock"] += 30 * 24 * 3600
        c.state["comments"].append(c.comment(102, "Sales reviews. Operations may reassign."))
        for _ in range(3):
            await c.service.step(c.repository)
        row = next(row for row in c.db["inbox"].values() if row.source_key == "comment:102")
        assert row.status == "received", row.data.get("reason")
        answered = await c.collaboration.process(row.data["id"])
        assert answered["natural_turn"]["resolution"]["state"] == "conversation_answer"
        assert answered["natural_turn"]["resolution"]["question_id"] == "natural-" + receipt
        assert answered["feedback_status"] == "sent" and not c.db["journal"]
        assert await type(service)(c.collaboration).process(row.data["id"]) == answered
        assert c.state["natural_calls"] == 2
        c.state["clock"] += 3600
        c.state["comments"].append(c.comment(103, "A duplicate answer without a new question."))
        for _ in range(3):
            await c.service.step(c.repository)
        duplicate = next(row for row in c.db["inbox"].values() if row.source_key == "comment:103")
        assert duplicate.status == "ignored" and c.state["natural_calls"] == 2


async def test_natural_status_wording_uses_the_configured_typed_route_instead_of_regex_run_selection(
    poll_case, monkeypatch
):  # noqa: F811
    async with poll_case() as c:
        service, _ = await prepared(c, monkeypatch, intent="status")
        c.state["clock"] += 3600
        c.state["comments"].append(c.comment(111, "@forge, what is the status?"))
        for _ in range(3):
            await c.service.step(c.repository)
        row = next(row for row in c.db["inbox"].values() if row.source_key == "comment:111")
        result = await c.collaboration.process(row.data["id"])
        assert result.get("natural_turn"), "Status wording bypassed the configured typed natural route"
        assert result["natural_turn"]["resolution"]["state"] == "status"
        assert c.state["natural_calls"] == 1 and result["feedback_status"] == "sent"


async def test_old_pending_product_question_is_not_lost_behind_two_hundred_later_ticket_receipts(
    poll_case, monkeypatch
):  # noqa: F811
    from uuid import uuid4

    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        await product_context(c, service, receipt)
        original = next(row for row in c.db["inbox"].values() if row.status == "processed")
        for sequence in range(300, 510):
            row = copy.deepcopy(original)
            row.id = uuid4()
            row.sequence = sequence
            row.data.update(id=row.id.hex, run_id=None, actor="99", command={"action": "status"})
            c.db["inbox"][row.id.hex] = row
        result = await service.process(receipt)
        assert result["natural_turn"]["resolution"]["state"] == "product_answer"
        assert c.state["natural_calls"] == 1


@pytest.mark.parametrize("change", ["question_body", "human_body", "membership"])
async def test_unmentioned_reply_rechecks_current_question_source_and_reader_before_any_model_call(
    poll_case,
    monkeypatch,
    change,  # noqa: F811
):  # noqa: F811
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        await product_context(c, service, receipt)
        c.state["clock"] += 3600
        c.state["comments"].append(c.comment(101, "Sales reviews. Operations may reassign."))
        for _ in range(3):
            await c.service.step(c.repository)
        row = next(row for row in c.db["inbox"].values() if row.source_key == "comment:101")
        assert row.status == "received"
        if change == "membership":
            c.db["memberships"]["member"].active = False
        else:
            number = 100 if change == "question_body" else 101
            next(comment for comment in c.state["comments"] if comment["id"] == number)["body"] += " edited"
        result = await c.collaboration.process(row.data["id"])
        assert result["status"] == "denied" and result["action_status"] == "not_dispatched"
        assert c.state["natural_calls"] == 0 and not c.db["journal"]
