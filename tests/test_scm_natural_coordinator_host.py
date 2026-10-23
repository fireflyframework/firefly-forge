# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The coordinator routes work to its native architect and grounds ordinary question references."""

import pytest

from firefly_forge.scm_natural_models import NaturalDecision, resolve_task

SCOPE = dict(
    project_id="a" * 32,
    repository_id="b" * 32,
    actor="7",
    issue={"kind": "issue", "number": 3},
    binding_digest="c" * 64,
    native_user_id="d" * 32,
)


def offered(language):
    questions = (
        ["Who can change warehouse permissions?", "Who reviews contact exports?"]
        if language == "en"
        else ["¿Quién cambia los permisos del almacén?", "¿Quién revisa las exportaciones de contactos?"]
    )
    return [
        {
            **SCOPE,
            "origin": "product_assistant_turn",
            "question_id": f"product-{index}",
            "question_digest": str(index) * 64,
            "conversation_id": "e" * 32,
            "turn_id": str(index) * 32,
            "question": question,
            "question_status": "pending",
            "consumed": False,
        }
        for index, question in enumerate(questions, 1)
    ]


def test_product_requests_handoff_without_creating_execution_authority():
    result = resolve_task(
        SCOPE,
        [],
        NaturalDecision(intent="product", message="I will send this to the product architect."),
        answer="Build a CRM with contact imports.",
    )
    assert result.state == "product_request"
    assert result.answer == "Build a CRM with contact imports."
    assert result.run_id is None and result.conversation_id is None


@pytest.mark.parametrize(
    "language,human,human_span,question_span",
    [
        (
            "en",
            "About warehouse permissions, only administrators may change them.",
            "warehouse permissions",
            "warehouse permissions",
        ),
        (
            "es",
            "Sobre los permisos del almacén, solo los administradores pueden cambiarlos.",
            "permisos del almacén",
            "permisos del almacén",
        ),
    ],
)
async def test_grounded_natural_reference_can_select_without_numbers_or_copying_question(
    language, human, human_span, question_span
):
    rows = offered(language)
    previous = resolve_task(SCOPE, rows, NaturalDecision(intent="clarify", message="Which question?"), answer="Yes")
    result = resolve_task(
        SCOPE,
        rows,
        NaturalDecision(
            intent="answer",
            message="I retained your answer.",
            choice=1,
            selection_evidence={"human_span": human_span, "question_span": question_span},
        ),
        answer=human,
        previous=previous.model_dump(mode="json"),
    )
    assert result.state == "product_answer" and result.question_id == "product-1"
    assert result.answer == human


@pytest.mark.parametrize("evidence", [None, {"human_span": "warehouse", "question_span": "warehouse"}])
def test_bare_yes_cannot_be_resolved_by_model_choice_or_manufactured_span(evidence):
    rows = offered("en")
    previous = resolve_task(SCOPE, rows, NaturalDecision(intent="clarify", message="Which question?"), answer="Yes")
    result = resolve_task(
        SCOPE,
        rows,
        NaturalDecision(intent="answer", message="Retained", choice=1, selection_evidence=evidence),
        answer="Yes",
        previous=previous.model_dump(mode="json"),
    )
    assert result.state == "waiting"
