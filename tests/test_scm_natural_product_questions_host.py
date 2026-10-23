# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Product questions originate in typed completed turns and confirmed publication, never prose."""

import copy
import hashlib
import importlib
import importlib.util

import pytest

from firefly_forge.communication import assistant_communication, communication_facts
from firefly_forge.journal import digest


def fixture():
    session = {"id": "a" * 32, "repository_id": "b" * 32, "project_id": "c" * 32}
    turn = {
        "id": "d" * 32,
        "session_id": session["id"],
        "status": "completed",
        "reply": {
            "intent": "dialogue",
            "message": "Public product proposal",
            "questions": ["Who reviews proposals?", "Which team may reassign work?"],
            "assumptions": [],
            "proposal": None,
            "citation_ids": [],
        },
    }
    events = []
    for kind, payload in [
        ("conversation_created", {"model_selection_digest": None}),
        ("turn_completed", {"turn_id": turn["id"], "status": "completed", "reply": turn["reply"]}),
        ("turn_failed", {"turn_id": "e" * 32, "status": "failed"}),
    ]:
        event = {
            "sequence": len(events) + 1,
            "previous": events[-1]["digest"] if events else "0" * 64,
            "kind": kind,
            "payload": payload,
            "at": "2026-10-23T05:00:00Z",
        }
        events.append({**event, "digest": digest(event)})
    session.update(journal_head=events[-1]["digest"], journal_sequence=len(events))
    publication = {
        "id": "f" * 32,
        "repository_id": session["repository_id"],
        "actor": "7",
        "issue": {"kind": "issue", "number": 4},
        "status": "processed",
        "action_status": "completed",
        "command": {"action": "assistant", "operation": "status"},
        "action_result": {
            "conversation_id": session["id"],
            "turns": [
                {
                    "turn_id": turn["id"],
                    "status": "completed",
                    "reply": turn["reply"]["message"],
                    "questions": turn["reply"]["questions"],
                    "proposal_reviewed": False,
                },
                {"turn_id": "e" * 32, "status": "failed", "reply": None, "questions": [], "proposal_reviewed": False},
            ],
        },
        "feedback_status": "sent",
        "feedback_remote": {"id": 100},
    }
    body = "A confirmed quote with the two typed questions."
    publication["feedback_intent"] = {
        "body": body,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "communication": {"facts": communication_facts(None, assistant=assistant_communication(publication))},
    }
    return publication, session, turn, events


def project(*args):
    assert importlib.util.find_spec("firefly_forge.scm_natural_questions"), (
        "Typed published question projection is missing"
    )
    return importlib.import_module("firefly_forge.scm_natural_questions").product_questions(*args)


def test_two_typed_product_questions_remain_bound_to_completed_turn_despite_later_failure():
    result = project(*fixture())
    assert result["origin"] == "product_assistant_turn"
    assert result["conversation_id"] == "a" * 32 and result["turn_id"] == "d" * 32
    assert len(result["questions"]) == 2 and result["publication"]["comment_id"] == "100"
    assert result.get("run_id") is None


@pytest.mark.parametrize(
    "change", ["unconfirmed", "wrong_turn", "changed_questions", "changed_body", "broken_journal", "failed_producer"]
)
def test_question_projection_refuses_unconfirmed_or_different_producer(change):
    publication, session, turn, events = fixture()
    if change == "unconfirmed":
        publication["feedback_status"] = "writing"
    elif change == "wrong_turn":
        turn["id"] = "9" * 32
    elif change == "changed_questions":
        turn = copy.deepcopy(turn)
        turn["reply"]["questions"] = ["Injected question?"]
    elif change == "changed_body":
        publication["feedback_intent"]["body"] += " changed"
    elif change == "broken_journal":
        events[1]["payload"] = {}
    else:
        turn["status"] = "failed"
    with pytest.raises(ValueError):
        project(publication, session, turn, events)


def test_questions_in_untrusted_prose_are_not_parsed_as_typed_pending_questions():
    publication, session, turn, events = fixture()
    turn["reply"]["questions"] = []
    # The message may contain punctuation; an empty typed question list is still empty.
    turn["reply"]["message"] = "Who owns this? Should it execute?"
    for event in events:
        if event["kind"] == "turn_completed":
            event["payload"]["reply"] = copy.deepcopy(turn["reply"])
    previous = "0" * 64
    for event in events:
        event["previous"] = previous
        event["digest"] = digest({k: v for k, v in event.items() if k != "digest"})
        previous = event["digest"]
    session["journal_head"] = previous
    publication["action_result"]["turns"][0].update(reply=turn["reply"]["message"], questions=[])
    publication["feedback_intent"]["communication"]["facts"] = communication_facts(
        None, assistant=assistant_communication(publication)
    )
    assert project(publication, session, turn, events) is None


def natural_fixture():
    receipt, _, _, _ = fixture()
    receipt["command"] = {"adapter": "addressed_status_v1"}
    receipt["digest"] = "1" * 64
    receipt["natural_turn"] = {
        "source_digest": receipt["digest"],
        "policy_digest": "2" * 64,
        "classifier_outcome": "confirmed_valid",
        "human_message": "Design review ownership.",
        "decision": {
            "intent": "dialogue",
            "message": "A typed design discussion.",
            "questions": ["Who reviews?", "Who may reassign?"],
        },
        "reply": "A typed design discussion.",
        "context": {
            "scope": {
                "project_id": "c" * 32,
                "repository_id": "b" * 32,
                "actor": "7",
                "issue": {"kind": "issue", "number": 4},
                "binding_digest": "3" * 64,
            }
        },
    }
    return receipt


def test_new_natural_questions_require_confirmed_typed_reply_and_its_exact_publication_digest():
    module = importlib.import_module("firefly_forge.scm_natural_questions")
    assert hasattr(module, "natural_questions"), "Typed questions from the generic conversation are not durable"
    receipt = natural_fixture()
    receipt["feedback_intent"]["natural_turn_digest"] = digest(module.natural_producer(receipt))
    result = module.natural_questions(receipt)
    assert result["origin"] == "natural_turn" and result["question_id"] == "natural-" + receipt["id"]
    assert result["questions"] == ["Who reviews?", "Who may reassign?"]
    receipt["natural_turn"]["decision"]["questions"] = ["Question edited after publication"]
    with pytest.raises(ValueError):
        module.natural_questions(receipt)


def test_a_clarification_about_offered_questions_does_not_create_a_new_competing_question():
    module = importlib.import_module("firefly_forge.scm_natural_questions")
    receipt = natural_fixture()
    receipt["natural_turn"]["resolution"] = {
        "state": "waiting",
        "candidates": [{"question_id": "first"}, {"question_id": "second"}],
    }
    receipt["feedback_intent"]["natural_turn_digest"] = digest(module.natural_producer(receipt))
    assert module.natural_questions(receipt) is None
