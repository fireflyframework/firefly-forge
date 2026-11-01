# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A semantic affirmative selects native published scope, never model-supplied authority."""

import copy

import pytest

from firefly_forge.scm_natural_models import NaturalDecision, resolve_task


def scope():
    return dict(
        repository_id="a" * 32,
        project_id="b" * 32,
        issue={"kind": "issue", "number": 4},
        actor="42",
        binding_digest="c" * 64,
        native_user_id="d" * 32,
    )


def review(number="e"):
    return dict(
        **scope(),
        origin="natural_turn",
        run_id=None,
        question_id="natural-" + number * 32,
        question_digest="f" * 64,
        question="Run all seven checks without coding or resetting history?",
        question_status="pending",
        consumed=False,
        verification_proposal_id=number * 32,
        verification_run_id="1" * 32,
    )


def approve(text="sí, ejecuta esas comprobaciones"):
    return NaturalDecision(
        intent="verify",
        message="Confirmaré las comprobaciones indicadas.",
        language="es",
        verification_consent="approve",
        verification_affirmative_span=text,
    )


def test_unique_published_review_accepts_plain_natural_affirmative_without_magic_phrases():
    result = resolve_task(scope(), [review()], approve(), answer="sí, ejecuta esas comprobaciones")
    assert result.state == "verification_confirm"
    assert result.run_id == "1" * 32 and result.question_id == "natural-" + "e" * 32
    assert result.answer == "sí, ejecuta esas comprobaciones"


@pytest.mark.parametrize(
    "mutation",
    [
        {"actor": "43"},
        {"binding_digest": "0" * 64},
        {"consumed": True},
        {"question_status": "answered"},
        {"verification_proposal_id": None},
    ],
)
def test_foreign_stale_consumed_or_nonverification_question_cannot_become_grant(mutation):
    result = resolve_task(scope(), [review() | mutation], approve(), answer="sí, ejecuta esas comprobaciones")
    assert result.state == "waiting" and result.run_id is None


def test_multiple_pending_reviews_require_clarification_even_when_model_returns_choice():
    decision = approve().model_copy(update={"choice": 2})
    result = resolve_task(scope(), [review(), review("2")], decision, answer="sí, ejecuta esas comprobaciones")
    assert result.state == "waiting" and result.reason == "verification_review_ambiguous"


def test_model_cannot_manufacture_affirmative_quote_or_use_legacy_answer_path():
    row = review()
    result = resolve_task(scope(), [row], approve("run everything"), answer="no, no ejecutes nada")
    assert result.state == "waiting"
    result = resolve_task(scope(), [row], NaturalDecision(intent="answer", message="Retained."), answer="yes")
    assert result.state == "waiting"


def test_verification_interpretation_has_no_ids_budgets_or_privileged_actor_fields():
    schema = NaturalDecision.model_json_schema()["properties"]
    assert not {"request_id", "run_id", "effect_budget", "global_admin", "candidate_digest"} & schema.keys()
    with pytest.raises(ValueError):
        NaturalDecision.model_validate(approve().model_dump() | {"request_id": "f" * 32})


def test_ordinary_question_resolution_is_unchanged():
    row = copy.deepcopy(review())
    row.pop("verification_proposal_id")
    row.pop("verification_run_id")
    result = resolve_task(scope(), [row], NaturalDecision(intent="answer", message="Retained."), answer="yes")
    assert result.state == "conversation_answer"


@pytest.mark.parametrize("origin", ["product_assistant_turn", "worker_journal", "natural_turn"])
def test_bare_yes_with_another_pending_purpose_cannot_authorize_checks(origin):
    other = review("3")
    other.pop("verification_proposal_id")
    other.pop("verification_run_id")
    other.update(origin=origin, question="Should existing customer contacts be retained?")
    original = copy.deepcopy(other)
    result = resolve_task(scope(), [review(), other], approve("sí"), answer="sí")
    assert result.state == "waiting" and result.reason == "explicit_verification_reference_required"
    assert other == original


def test_explicit_natural_check_reference_with_competing_product_question_keeps_other_question():
    from firefly_forge.scm_natural_models import GroundedSelection

    other = review("3")
    other.pop("verification_proposal_id")
    other.pop("verification_run_id")
    other.update(origin="product_assistant_turn", question="Should customer contacts be retained?")
    original = copy.deepcopy(other)
    # question_choices sorts these native references, not the untrusted model's IDs.
    decision = approve().model_copy(
        update={
            "choice": 2,
            "selection_evidence": GroundedSelection(
                human_span="ejecuta esas comprobaciones", question_span="Run all seven checks"
            ),
        }
    )
    result = resolve_task(scope(), [review(), other], decision, answer="sí, ejecuta esas comprobaciones")
    assert result.state == "verification_confirm" and other == original


@pytest.mark.parametrize("human_length", [5000, 12000])
def test_accepted_natural_confirmation_preserves_existing_intake_limit(human_length):
    import hashlib

    from firefly_forge.journal import digest
    from firefly_forge.scm_conversation_models import addressed_status
    from firefly_forge.scm_linked_verification_records import SCMVerificationConfirmationV1
    from firefly_forge.scm_natural_replies import human_message

    prefix = "sí, ejecuta esas comprobaciones. "
    text = prefix + "x" * (human_length - len(prefix))
    source = "@forge, " + text
    addressed = addressed_status(source)
    assert addressed is not None and human_message(source, addressed) == text
    decision = approve(text)
    assert decision.verification_affirmative_span == text
    value = dict(
        version=1,
        review_digest="a" * 64,
        receipt_id="b" * 32,
        source_digest="c" * 64,
        human_message=text,
        human_sha256=hashlib.sha256(text.encode()).hexdigest(),
        affirmative_span=text,
        interpretation_digest=digest(decision.model_dump(mode="json")),
        intake_digest="d" * 64,
    )
    model = SCMVerificationConfirmationV1.model_validate({**value, "digest": digest(value)})
    assert model.human_message == text and model.affirmative_span == text
