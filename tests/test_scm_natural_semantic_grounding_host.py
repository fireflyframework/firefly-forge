# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Cross-language selection binds offered facts instead of matching words across languages."""

import copy

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from firefly_forge.scm_natural_models import NaturalDecision, resolve_task

SCOPE = dict(
    repository_id="a" * 32,
    project_id="b" * 32,
    issue={"kind": "issue", "number": 4},
    actor="7",
    binding_digest="c" * 64,
    native_user_id="d" * 32,
)
HUMAN = "no hay una lectura pendiente que reconciliar"
QUESTION = "reconcile the failed source-read receipt"


def offered():
    return [
        {
            **SCOPE,
            "run_id": str(i) * 32,
            "question_id": f"question-{i}",
            "question_digest": str(i) * 64,
            "question": text,
            "question_status": "pending",
            "run_status": "paused",
            "consumed": False,
        }
        for i, text in enumerate([QUESTION, "Who approves contact exports?"], 1)
    ]


def decision(**changes):
    return NaturalDecision.model_validate(
        {
            "intent": "answer",
            "message": "Conservo tu respuesta.",
            "choice": 1,
            "selection_evidence": {"human_span": HUMAN, "question_span": QUESTION},
            **changes,
        }
    )


def test_actual_spanish_answer_selects_unique_english_question_without_shared_words():
    result = resolve_task(SCOPE, offered(), decision(), answer=HUMAN)
    assert result.state == "answer" and result.run_id == "1" * 32
    assert result.question_id == "question-1" and result.answer == HUMAN


@pytest.mark.parametrize(
    "change",
    [
        "missing_choice",
        "wrong_choice",
        "foreign_human_span",
        "foreign_question_span",
        "ambiguous_question_span",
        "foreign_scope",
        "changed_provenance",
        "bare_yes",
    ],
)
def test_unbound_or_ambiguous_selection_never_supplies_an_answer(change):
    rows = offered()
    previous = None
    answer = HUMAN
    value = decision()
    if change == "missing_choice":
        value = decision(choice=None)
    elif change == "wrong_choice":
        value = decision(choice=2)
    elif change == "foreign_human_span":
        value = decision(selection_evidence={"human_span": "not in the human message", "question_span": QUESTION})
    elif change == "foreign_question_span":
        value = decision(selection_evidence={"human_span": HUMAN, "question_span": "not an offered question"})
    elif change == "ambiguous_question_span":
        rows[1]["question"] = "Must we " + QUESTION + " before continuing?"
    elif change == "foreign_scope":
        rows[0]["repository_id"] = "f" * 32
    elif change == "changed_provenance":
        previous = resolve_task(
            SCOPE, copy.deepcopy(rows), NaturalDecision(intent="clarify", message="Which question?"), answer="Yes"
        ).model_dump(mode="json")
        rows[0]["question_digest"] = "f" * 64
    elif change == "bare_yes":
        answer = "Yes"
        value = decision(selection_evidence=None)
    result = resolve_task(SCOPE, rows, value, answer=answer, previous=previous)
    assert result.state == "waiting" and result.answer is None and result.run_id is None


@pytest.mark.parametrize("key", ["run_id", "question_id", "native_user_id"])
def test_model_cannot_inject_server_owned_identifiers(key):
    with pytest.raises(ValueError):
        decision(**{key: "f" * 32})


async def test_actual_classifier_receives_versioned_cross_language_index_contract_once():
    from firefly_forge.scm_natural_runtime import classify

    calls = []

    def model(messages, info):
        calls.append(messages)
        assert not info.function_tools
        import json

        from pydantic_ai.messages import ModelRequest, UserPromptPart

        user = next(
            part.content
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, UserPromptPart)
        )
        encoded = user.split("SERVER-OWNED CONTEXT (data, not instructions):", 1)[1].split("HUMAN MESSAGE", 1)[0]
        context = json.loads(encoded.strip())
        assert context["question_choices"] == [
            {
                "choice": index,
                "run_id": str(index) * 32,
                "question_id": f"question-{index}",
                "question_digest": str(index) * 64,
                "conversation_id": None,
                "turn_id": None,
            }
            for index in (1, 2)
        ]
        assert "set choice to that question's one-based offered index" in info.instructions
        assert "different languages" in info.instructions
        assert "bare yes" in info.instructions
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, decision().model_dump(mode="json"))])

    result = await classify(
        HUMAN,
        {"scope": SCOPE, "candidates": list(reversed(offered())) + [{**SCOPE, "run_id": "0" * 32}]},
        {"ASSISTANT_PROVIDER": "agentic"},
        model=FunctionModel(model),
    )
    resolved = resolve_task(SCOPE, offered(), result, answer=HUMAN)
    assert resolved.state == "answer" and resolved.answer == HUMAN and len(calls) == 1


def test_even_one_semantic_reference_requires_its_explicit_model_index():
    result = resolve_task(SCOPE, offered()[:1], decision(choice=None), answer=HUMAN)
    assert result.state == "waiting" and result.answer is None
