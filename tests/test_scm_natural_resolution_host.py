# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Natural wording never chooses an unbound execution or consumes an answer twice."""

import copy
import importlib
import importlib.util

import pytest


def implementation():
    assert importlib.util.find_spec("firefly_forge.scm_natural_models"), "Natural task resolution is missing"
    return importlib.import_module("firefly_forge.scm_natural_models")


def context():
    return {
        "repository_id": "a" * 32,
        "project_id": "b" * 32,
        "issue": {"kind": "issue", "number": 7},
        "actor": "42",
        "binding_digest": "c" * 64,
    }


def candidate(run="d", *, status="pending"):
    return {
        **context(),
        "run_id": run * 32,
        "question_id": "owner-choice",
        "question_digest": "e" * 64,
        "question": "Which team owns review?",
        "question_status": status,
        "run_status": "paused",
        "response_digest": "f" * 64 if status == "answered" else None,
        "consumed": False,
    }


def decide(intent, candidates, *, previous=None, choice=None, answer="Sales owns review."):
    module = implementation()
    return module.resolve_task(
        context(),
        candidates,
        module.NaturalDecision(intent=intent, message="Public reply", choice=choice),
        answer=answer,
        previous=previous,
    )


def test_delayed_answer_resolves_same_durable_question_after_process_reconstruction():
    first = decide("clarify", [candidate()])
    retained = copy.deepcopy(first.model_dump(mode="json"))
    result = decide("answer", [candidate()], previous=retained)
    assert result.state == "answer" and result.run_id == "d" * 32
    assert result.question_id == "owner-choice" and result.answer == "Sales owns review."


def test_multiple_pending_questions_wait_until_human_selects_offered_choice():
    held = decide("answer", [candidate(), candidate("9")])
    assert held.state == "waiting" and held.run_id is None and len(held.candidates) == 2
    selected = decide(
        "answer",
        [candidate(), candidate("9")],
        previous=held.model_dump(mode="json"),
        choice=2,
        answer="The second question. Sales owns review.",
    )
    assert selected.state == "answer" and selected.run_id == "d" * 32


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository_id", "9" * 32),
        ("project_id", "9" * 32),
        ("issue", {"kind": "issue", "number": 8}),
        ("actor", "99"),
        ("binding_digest", "9" * 64),
    ],
)
def test_cross_issue_nonowner_or_changed_binding_never_becomes_answer(field, value):
    other = candidate()
    other[field] = value
    result = decide("answer", [other])
    assert result.state == "waiting" and result.run_id is None


def test_changed_question_cannot_inherit_an_earlier_clarification_selection():
    held = decide("clarify", [candidate()]).model_dump(mode="json")
    changed = candidate()
    changed["question_digest"] = "0" * 64
    result = decide("answer", [changed], previous=held)
    assert result.state == "waiting" and result.reason == "context_changed"


@pytest.mark.parametrize(
    "update",
    [
        {"question_status": "answered", "response_digest": "f" * 64},
        {"consumed": True},
        {"run_status": "queued"},
    ],
)
def test_late_or_already_consumed_answer_never_reopens_original_task(update):
    row = candidate()
    row.update(update)
    result = decide("answer", [row])
    assert result.state == "waiting" and result.answer is None


def test_resume_requires_exact_answered_unconsumed_question():
    assert decide("resume", [candidate()]).state == "waiting"
    result = decide("resume", [candidate(status="answered")])
    assert result.state == "resume" and result.response_digest == "f" * 64


def test_model_cannot_supply_a_run_or_question_identifier_or_rewrite_human_answer():
    module = implementation()
    with pytest.raises(ValueError):
        module.NaturalDecision(intent="answer", message="Reply", run_id="9" * 32)
    assert decide("answer", [candidate()], answer="Exact owner's text.").answer == "Exact owner's text."


def test_reordered_candidates_do_not_change_the_persisted_numbered_choice():
    rows = [candidate(), candidate("9")]
    retained = decide("clarify", rows).model_dump(mode="json")
    selected = decide(
        "answer", list(reversed(rows)), previous=retained, choice=1, answer="The first question. Sales reviews."
    )
    assert selected.run_id == "9" * 32


def test_malformed_or_out_of_range_choice_is_not_guessed():
    module = implementation()
    with pytest.raises(ValueError):
        module.NaturalDecision(intent="answer", message="Reply", choice=True)
    result = decide("answer", [candidate()], choice=2)
    assert result.state == "waiting" and result.run_id is None


def test_product_question_answer_keeps_original_completed_turn_without_a_worker_run():
    from firefly_forge.scm_natural_models import NaturalDecision, resolve_task

    scope = dict(
        repository_id="a" * 32,
        project_id="b" * 32,
        issue={"kind": "issue", "number": 4},
        actor="7",
        binding_digest="c" * 64,
    )
    row = {
        **scope,
        "origin": "product_assistant_turn",
        "conversation_id": "d" * 32,
        "turn_id": "e" * 32,
        "run_id": None,
        "question_id": "product-" + "e" * 32,
        "question_digest": "f" * 64,
        "question_status": "pending",
        "consumed": False,
    }
    result = resolve_task(
        scope,
        [row],
        NaturalDecision(intent="answer", message="Gracias."),
        answer="Ventas revisa; operaciones reasigna.",
    )
    assert result.state == "product_answer" and result.run_id is None
    assert result.question_id == row["question_id"] and result.answer == "Ventas revisa; operaciones reasigna."
    assert result.conversation_id == row["conversation_id"] and result.turn_id == row["turn_id"]
    resume = resolve_task(scope, [row], NaturalDecision(intent="resume", message="Continue."), answer="Continue.")
    assert resume.state == "waiting" and resume.reason == "product_answer_has_no_execution_authority"


def test_parallel_product_and_worker_questions_require_human_disambiguation():
    from firefly_forge.scm_natural_models import NaturalDecision, resolve_task

    scope = dict(
        repository_id="a" * 32,
        project_id="b" * 32,
        issue={"kind": "issue", "number": 4},
        actor="7",
        binding_digest="c" * 64,
    )
    product = {
        **scope,
        "origin": "product_assistant_turn",
        "run_id": None,
        "question_id": "product-" + "d" * 32,
        "conversation_id": "e" * 32,
        "turn_id": "d" * 32,
        "question_digest": "f" * 64,
        "question_status": "pending",
        "consumed": False,
    }
    worker = {
        **scope,
        "run_id": "1" * 32,
        "question_id": "2" * 32,
        "question_digest": "3" * 64,
        "run_status": "paused",
        "question_status": "pending",
        "consumed": False,
    }
    result = resolve_task(
        scope, [product, worker], NaturalDecision(intent="answer", message="Which question?"), answer="Yes"
    )
    assert result.state == "waiting" and len(result.candidates) == 2


def test_natural_model_can_retain_typed_questions_but_cannot_mark_a_proposal_reviewed():
    from firefly_forge.scm_natural_models import NaturalDecision

    result = NaturalDecision(
        intent="dialogue",
        message="Two points to confirm.",
        questions=["Who reviews?", "Who may reassign?"],
        language="es",
    )
    assert len(result.questions) == 2 and result.language == "es"
    with pytest.raises(ValueError):
        NaturalDecision(intent="dialogue", message="Invalid question.", questions=[" "])
    with pytest.raises(ValueError):
        NaturalDecision(
            intent="dialogue",
            message="Already reviewed.",
            proposal={
                "customer": "Sales",
                "problem": "Review work",
                "journey": ["Review"],
                "user_stories": ["As an owner, I review"],
                "acceptance": ["Review retained"],
                "owners": ["Sales"],
                "functional_reviewed": True,
            },
        )


def test_a_model_choice_alone_cannot_resolve_an_ambiguous_human_yes():
    held = decide("answer", [candidate(), candidate("9")])
    result = decide(
        "answer", [candidate(), candidate("9")], previous=held.model_dump(mode="json"), choice=2, answer="Yes"
    )
    assert result.state == "waiting" and result.run_id is None
