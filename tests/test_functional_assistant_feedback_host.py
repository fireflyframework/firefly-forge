# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Confirmed functional replies reach the real feedback writer and retained comment."""

import json

import pytest
from communication_model_fixture import reviewed_model
from pydantic_ai.messages import ModelResponse, ToolCallPart
from test_communication_writer import configured_store
from test_scm_delegated_feedback import RECEIPT, received
from test_scm_delegated_feedback import delegated_feedback as delegated_feedback

CONVERSATION, TURN = "a" * 32, "b" * 32


def completed(row, operation, result):
    received(row, "status")
    row.status = "processed"
    row.data.update(
        status="processed",
        action_status="completed",
        feedback_status="pending",
        command=dict(action="assistant", operation=operation, language="es"),
        action_result=result,
    )
    row.data.pop("run_id", None)


@pytest.mark.parametrize("operation", ["start", "turn", "proposal"])
async def test_configured_feedback_grounds_actual_assistant_outcome_and_preserves_reply(
    delegated_feedback, tmp_path, operation
):
    service, row, _, sent, _ = delegated_feedback
    service.store.task_router = (await configured_store(tmp_path)).task_router
    result = dict(conversation_id=CONVERSATION, status="conversation_created", model_invoked=False)
    if operation != "start":
        result.update(
            turn_id=TURN,
            status="completed",
            reply="Podemos definir el recorrido del cliente.\n\nPrimero acordemos el alcance.",
            questions=["¿Qué equipo atiende al cliente?", "¿Quién aprueba el alcance?"],
            proposal_reviewed=False,
        )
        result.pop("model_invoked")
    if operation == "proposal":
        result["proposal_status"] = "unreviewed"
    completed(row, "start" if operation == "start" else "turn", result)
    authorizations = []

    async def authorize(data, **kwargs):
        assert data["id"] == RECEIPT
        authorizations.append(data["id"])

    service._feedback_authority = authorize
    observed = []

    def draft(messages, info):
        observed.append(messages)
        assert "conversation_created" in str(messages) if operation == "start" else "Primero acordemos" in str(messages)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "title": {"text": "Conversación funcional", "fact_ids": ["assistant"]},
                        "paragraphs": [
                            {
                                "text": "La conversación contiene el resultado confirmado de la solicitud.",
                                "fact_ids": ["assistant"],
                            }
                        ],
                    },
                )
            ]
        )

    service.communication_writer.model = reviewed_model(draft)
    await service.feedback(RECEIPT)
    assert len(sent) == 1, row.data
    assert row.data["feedback_status"] == "sent"
    facts = row.data["feedback_intent"]["communication"]["facts"]
    assert set(facts) == {"assistant"}
    assert facts["assistant"]["conversation_id"] == CONVERSATION
    assert facts["assistant"]["status"] == result["status"]
    assert "unknown" not in json.dumps(facts)
    assert observed and len(authorizations) >= 2
    if operation != "start":
        assert "Podemos definir el recorrido del cliente" in sent[0]["body"]
        assert "Primero acordemos el alcance" in sent[0]["body"]
        assert "¿Qué equipo atiende al cliente?" in sent[0]["body"]
        assert "¿Quién aprueba el alcance?" in sent[0]["body"]
    if operation == "proposal":
        assert facts["assistant"]["proposal_status"] == "unreviewed"
        assert "unreviewed proposal" in sent[0]["body"]


@pytest.mark.parametrize("change", ["unconfirmed", "model_claim", "invalid_id", "coerced_bool", "approval"])
def test_only_confirmed_typed_assistant_results_can_ground_feedback(change):
    from types import SimpleNamespace

    from firefly_forge.communication import assistant_communication

    row = SimpleNamespace()
    result = dict(conversation_id=CONVERSATION, status="conversation_created", model_invoked=False)
    completed(row, "start", result)
    if change == "unconfirmed":
        row.data["action_status"] = "needs_reconciliation"
    elif change == "model_claim":
        result["model_invoked"] = True
    elif change == "invalid_id":
        result["conversation_id"] = "not-a-native-conversation"
    elif change == "coerced_bool":
        result["model_invoked"] = 0
    else:
        row.data["command"]["operation"] = "turn"
        result.update(status="completed", turn_id=TURN, reply="A public reply", proposal_reviewed=True)
    with pytest.raises(ValueError):
        assistant_communication(row.data)


@pytest.mark.parametrize(
    "result",
    [
        dict(conversation_id=CONVERSATION, turns=[]),
        dict(conversations=[]),
    ],
)
def test_assistant_status_is_a_retained_conversation_read_without_unknown_run(result):
    from types import SimpleNamespace

    from firefly_forge.communication import assistant_communication, communication_facts

    row = SimpleNamespace()
    completed(row, "status", result)
    facts = communication_facts(None, assistant=assistant_communication(row.data))
    assert set(facts) == {"assistant"}
    assert facts["assistant"]["operation"] == "status"
    assert facts["assistant"]["status"] == "read"
    assert "state" not in facts


async def test_assistant_outcome_changed_during_draft_is_held_without_comment(delegated_feedback, tmp_path):
    service, row, _, sent, _ = delegated_feedback
    service.store.task_router = (await configured_store(tmp_path)).task_router
    completed(
        row,
        "turn",
        dict(
            conversation_id=CONVERSATION,
            turn_id=TURN,
            status="completed",
            reply="Original public reply",
            questions=["Original question?"],
            proposal_reviewed=False,
        ),
    )

    async def authorize(data, **kwargs):
        return None

    service._feedback_authority = authorize

    def draft(messages, info):
        row.data["action_result"]["reply"] = "Newer operator-confirmed result"
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "title": {"text": "Functional conversation", "fact_ids": ["assistant"]},
                        "paragraphs": [{"text": "The confirmed reply is available.", "fact_ids": ["assistant"]}],
                    },
                )
            ]
        )

    service.communication_writer.model = reviewed_model(draft)
    await service.feedback(RECEIPT)
    assert sent == []
    assert row.data["feedback_status"] == "held"
    assert row.data["feedback_intent"]["communication"]["reason"] == "state_changed"
    assert row.data["feedback_intent"]["body"] == ""
    assert row.data["action_result"]["reply"] == "Newer operator-confirmed result"


async def test_assistant_reply_has_no_generic_unknown_fallback_when_communication_is_unconfigured(delegated_feedback):
    service, row, _, sent, _ = delegated_feedback
    completed(
        row,
        "turn",
        dict(
            conversation_id=CONVERSATION,
            turn_id=TURN,
            status="completed",
            reply="The confirmed public reply.",
            questions=["Which owner?"],
            proposal_reviewed=False,
        ),
    )

    async def authorize(data, **kwargs):
        return None

    service._feedback_authority = authorize
    await service.feedback(RECEIPT)
    assert len(sent) == 1
    assert "The confirmed public reply" in sent[0]["body"]
    assert "Which owner?" in sent[0]["body"]
    assert "unknown" not in sent[0]["body"].lower()
    assert row.data["feedback_intent"]["communication"]["reason"] == "unconfigured"


async def test_current_authority_still_refuses_assistant_publication_after_draft(delegated_feedback, tmp_path):
    from firefly_forge.project_access import AccessDenied

    service, row, _, sent, _ = delegated_feedback
    service.store.task_router = (await configured_store(tmp_path)).task_router
    completed(row, "start", dict(conversation_id=CONVERSATION, status="conversation_created", model_invoked=False))
    checks = []

    async def authorize(data, **kwargs):
        checks.append(data["id"])
        if len(checks) > 1:
            raise AccessDenied("Controlled authority revoked during communication")

    service._feedback_authority = authorize
    service.communication_writer.model = reviewed_model(
        {
            "title": {"text": "Functional conversation", "fact_ids": ["assistant"]},
            "paragraphs": [{"text": "The conversation was created.", "fact_ids": ["assistant"]}],
        }
    )
    await service.feedback(RECEIPT)
    assert sent == []
    assert row.data["feedback_status"] == "needs_reconciliation"
    assert len(checks) == 2
