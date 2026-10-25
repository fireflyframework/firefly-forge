# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Natural classification cannot turn historical prose into current execution evidence."""

import copy

import pytest
from communication_model_fixture import reviewed_model
from test_communication_factory_verification_host import paused
from test_communication_writer import configured_store
from test_scm_delegated_feedback import RECEIPT, RUN, received
from test_scm_delegated_feedback import delegated_feedback as delegated_feedback
from test_scm_natural_worker_context_host import fixture


async def test_authorized_candidate_projects_current_factory_facts_separately_from_history():
    natural, message, scopes, rows, runs = await fixture()
    current = paused()
    current.update(id="1" * 32)
    current["verification_handoff"].update(completed_commands=1, failed_commands=1)
    runs["1" * 32] = current
    rows[1].data["feedback_intent"]["body"] = "Old task exhausted its read quota. PR 6 was partial."
    context = await natural._context(message, scopes)
    selected = next(row for row in context["candidates"] if row["run_id"] == "1" * 32)
    assert context["status_context"]["selected_run_id"] == "1" * 32
    assert selected["current_facts"]["state"] == {"status": "paused"}
    assert selected["current_facts"]["factory_verification"]["checks"] == {
        "source": "recorded_handoff",
        "result": "not_completed",
        "total": 7,
        "recorded": 1,
        "failed": 1,
    }
    assert "read quota" not in str(selected["current_facts"])
    assert "blocker" not in selected["current_facts"]
    before = copy.deepcopy(context)
    runs["1" * 32]["verification_handoff"]["completed_commands"] = 2
    assert await natural._context(message, scopes) != before


@pytest.mark.parametrize("outcome", ["supported", "repair", "revoked", "progressed", "native_progressed"])
async def test_selected_natural_status_uses_reviewed_current_ai_card_not_classifier_text(
    delegated_feedback, tmp_path, outcome
):
    service, row, run, sent, state = delegated_feedback
    current = paused()
    current.update(id=RUN)
    current["verification_handoff"].update(completed_commands=1, failed_commands=1)
    run.update(current)
    received(row, "status")
    row.data.update(status="processed", action_status="completed", feedback_status="pending", run_id=RUN)
    row.data["digest"] = "d" * 64
    stale = "Old task exhausted read_file quota; PR 6 is the current result."
    row.data["natural_turn"] = {
        "classifier_outcome": "confirmed_valid",
        "source_digest": row.data["digest"],
        "policy_digest": "e" * 64,
        "human_message": "¿Cómo va la reparación?",
        "context": {"scope": {key: row.data[key] for key in ("repository_id", "actor", "issue")}},
        "state": "status",
        "decision": {"intent": "status", "language": "es", "message": stale},
        "resolution": {"state": "status", "run_id": RUN},
        "reply": stale,
    }
    service.store.task_router = (await configured_store(tmp_path)).task_router
    calls = []
    facts = [
        "state",
        "work",
        "requested_work",
        "execution_scope",
        "verification",
        "check_status",
        "factory_verification",
        "conversation",
    ]
    grounded = {
        "title": {"text": "Verificación pausada", "fact_ids": ["state"]},
        "paragraphs": [
            {
                "text": (
                    "La fábrica registró una de siete pruebas y falló. La verificación está retenida "
                    "y requiere revisión nativa; la reparación sigue pendiente."
                ),
                "fact_ids": facts[:4],
            },
            {
                "text": (
                    "La prueba anterior pertenece a otro intento; no acredita aceptación actual. "
                    "La siguiente acción es revisar la retención registrada."
                ),
                "fact_ids": facts[4:],
            },
        ],
    }
    initial = copy.deepcopy(grounded)
    if outcome == "repair":
        initial["paragraphs"][0]["text"] = "The current blocker is exhausted read quota from the old task."

    def candidate(messages, info):
        from pydantic_ai.messages import ModelResponse, ToolCallPart

        if outcome == "revoked":
            state["deny"] = True
        if outcome == "progressed":
            run["verification_handoff"]["completed_commands"] = 2
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, initial)])

    service.communication_writer.model = reviewed_model(
        candidate,
        verdict="unsupported" if outcome == "repair" else "supported",
        correction=grounded,
        calls=calls,
    )
    if outcome == "native_progressed":
        service.store.task_router = None
        original_write = service.communication_writer.write

        async def progressed(*args, **kwargs):
            result = await original_write(*args, **kwargs)
            run["verification_handoff"]["completed_commands"] = 2
            return result

        service.communication_writer.write = progressed
    await service.feedback(RECEIPT)
    if outcome == "native_progressed":
        assert calls == [] and row.data["feedback_status"] == "sent"
        assert row.data["feedback_intent"]["communication"]["natural_reply_source"] == "current_execution_facts"
        return
    assert [phase for phase, *_ in calls] == ["draft", "review"] + (["draft"] if outcome == "repair" else [])
    if outcome in {"revoked", "progressed"}:
        assert sent == []
        assert row.data["feedback_status"] in {"held", "needs_reconciliation"}
        return
    assert row.data["feedback_status"] == "sent" and len(sent) == 1
    assert "una de siete" in sent[0]["body"] and "read_file" not in sent[0]["body"]
    assert row.data["feedback_intent"]["communication"]["mode"] == "ai"
    assert "PR 6" not in str(calls[0][1])


def test_status_classifier_questions_do_not_become_unpublished_question_authority():
    import hashlib

    from firefly_forge.journal import digest
    from firefly_forge.scm_natural_questions import natural_producer, natural_questions

    reply = "Historical classifier wording with a follow-up."
    body = "Current reviewed execution status."
    receipt = {
        "id": "1" * 32,
        "digest": "2" * 64,
        "repository_id": "3" * 32,
        "actor": "7",
        "issue": {"kind": "issue", "number": 4},
        "status": "processed",
        "action_status": "completed",
        "feedback_status": "sent",
        "feedback_remote": {"id": 91},
    }
    receipt["natural_turn"] = {
        "classifier_outcome": "confirmed_valid",
        "source_digest": receipt["digest"],
        "policy_digest": "4" * 64,
        "human_message": "Current status?",
        "context": {"scope": {key: receipt[key] for key in ("repository_id", "actor", "issue")}},
        "state": "status",
        "decision": {"intent": "status", "message": reply, "questions": ["Unpublished classifier question?"]},
        "resolution": {"state": "status", "run_id": RUN},
        "reply": reply,
    }
    receipt["feedback_intent"] = {
        "body": body,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "natural_turn_digest": digest(natural_producer(receipt)),
        "communication": {"natural_reply_source": "current_execution_facts"},
    }
    assert natural_questions(receipt) is None
    receipt["feedback_intent"]["communication"] = {"reason": "natural_recorded_reply"}
    assert natural_questions(receipt)["questions"] == ["Unpublished classifier question?"]
