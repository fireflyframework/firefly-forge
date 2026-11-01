# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Readable AI updates retain exact diagnostic evidence and review custody."""

import copy
import hashlib
import json

import pytest
from communication_model_fixture import reviewed_model
from test_communication_writer import configured_store

from firefly_forge.communication import (
    CommunicationCard,
    CommunicationConversationV1,
    CommunicationReview,
    CommunicationWriter,
    _render_card,
    communication_facts,
)


def failed_check():
    return {
        "status": "failed",
        "attempt": 2,
        "final_verification": [
            {
                "argv": ["python", "-m", "pytest", "tests/test_validation.py"],
                "exit_code": 1,
                "attempt": 2,
                "output": "assert response.status_code == 422\nE assert 500 == 422",
            }
        ],
    }


def detail_card():
    return {
        "title": {"text": "La prueba de validación sigue fallando", "fact_ids": ["state", "verification"]},
        "paragraphs": [
            {
                "text": "La API devolvió 500 donde la prueba esperaba 422.",
                "heading": "Qué ha fallado",
                "fact_ids": ["check_diagnostics"],
                "diagnostic_id": "check-1-output",
            },
            {
                "text": "La comprobación terminó con un fallo.",
                "format": "bullet",
                "fact_ids": ["verification"],
            },
        ],
    }


def test_ai_selects_sections_bullets_and_exact_recorded_code_excerpt():
    run = failed_check()
    before = copy.deepcopy(run)
    facts = communication_facts(run)
    body = _render_card(CommunicationCard.model_validate(detail_card()), facts, lambda x: x, lambda _: None)
    assert "#### Qué ha fallado\n" in body
    assert "- La comprobación terminó con un fallo." in body
    assert "```text\nassert response.status_code == 422\nE assert 500 == 422\n```" in body
    assert facts["check_diagnostics"]["check-1-output"]["provenance"] == "current"
    assert run == before


@pytest.mark.parametrize("reference", ["check-2-output", "run-error", "https://attacker.test", "../private"])
def test_model_cannot_invent_or_redirect_an_excerpt(reference):
    card = detail_card()
    card["paragraphs"][0]["diagnostic_id"] = reference
    with pytest.raises(ValueError):
        _render_card(
            CommunicationCard.model_validate(card), communication_facts(failed_check()), lambda x: x, lambda _: None
        )


def test_diagnostic_projection_redacts_local_paths_settings_and_known_environment_secrets(monkeypatch):
    monkeypatch.setenv("FORGE_TEST_PASSWORD", "private-secret-fixture")
    run = failed_check()
    run["final_verification"][0]["output"] = (
        "File /Users/private/project/app.py\nDEBUG_VALUE=other-secret\n"
        "private-secret-fixture\n"
        "https://private.example.test/secret\n```\n@everyone"
    )
    facts = communication_facts(run)
    excerpt = facts["check_diagnostics"]["check-1-output"]["text"]
    for private in ["/Users/private", "other-secret", "private-secret-fixture", "https://private"]:
        assert private not in excerpt
    body = _render_card(CommunicationCard.model_validate(detail_card()), facts, lambda x: x, lambda _: None)
    assert "````text\n" in body


@pytest.mark.parametrize(
    "text",
    [
        "password: fixture-unregistered",
        "api_key=fixture-unregistered",
        'TOKEN="two words"',
        '"client_secret": "unknown-private-value"',
        "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----",
    ],
)
def test_sensitive_looking_output_is_withheld_instead_of_partially_published(text):
    run = failed_check()
    run["final_verification"][0]["output"] = text
    facts = communication_facts(run)
    assert "check-1-output" not in facts.get("check_diagnostics", {})
    assert "check-1-command" in facts["check_diagnostics"]


@pytest.mark.parametrize("change", [{"attempt": 3}, {"execution": "unknown"}, {"execution": "reused"}])
def test_inconsistent_or_unknown_diagnostic_provenance_stays_unknown(change):
    run = failed_check()
    run["final_verification"][0].update(change)
    assert communication_facts(run)["check_diagnostics"]["check-1-output"]["provenance"] == "unknown"


def test_historical_check_excerpt_does_not_become_current_after_restart():
    run = failed_check()
    run["attempt"] = 3
    assert communication_facts(run)["check_diagnostics"]["check-1-output"]["provenance"] == "retained"
    run["final_verification"][0].pop("attempt")
    assert communication_facts(run)["check_diagnostics"]["check-1-output"]["provenance"] == "unknown"


def test_selection_failure_has_a_useful_display_reason_without_claiming_root_cause_or_authority():
    facts = communication_facts(
        {"status": "failed", "error": "Original selection, configuration or reviewed policy epoch changed"}
    )
    assert facts["blocker"]["kind"] == "execution_selection_guard"
    assert facts["blocker"]["cause"] == "not_established"
    assert "execution_grant" not in facts["blocker"]


def test_semantic_editor_can_reject_opaque_or_badly_structured_wording():
    for issue in ["unclear_language", "poor_structure"]:
        assert CommunicationReview.model_validate(
            {
                "reader_checks": {
                    "answers_question": True,
                    "relevant_detail": True,
                    "no_redundancy": True,
                    "plain_language": False,
                    "useful_excerpts": True,
                },
                "verdict": "unsupported",
                "issues": [issue],
                "fact_ids": ["state"],
                "rationale": "The update obscures the available result.",
            }
        ).issues == [issue]


async def test_real_writer_repairs_opaque_copy_once_without_a_template_fallback(tmp_path):
    calls = []
    model = reviewed_model(
        {
            "title": {"text": "Resultado no aceptado", "fact_ids": ["state"]},
            "paragraphs": [{"text": "La unidad seleccionada no acredita una entrega.", "fact_ids": ["state"]}],
        },
        verdict="unsupported",
        issues=["unclear_language"],
        reader_checks={
            "answers_question": True,
            "relevant_detail": True,
            "no_redundancy": True,
            "plain_language": False,
            "useful_excerpts": True,
        },
        correction={
            "title": {"text": "La ejecución se ha detenido", "fact_ids": ["state"]},
            "paragraphs": [{"text": "Todavía no tengo un resultado de esta ejecución.", "fact_ids": ["state"]}],
        },
        calls=calls,
    )
    result = await CommunicationWriter(await configured_store(tmp_path, "bridge"), model=model).write(
        {"status": "failed"}, "Native fallback", lambda x: x
    )
    assert result["communication"]["mode"] == "ai"
    assert result["communication"]["repair_attempted"] is True
    assert "Native fallback" not in result["body"]
    assert "La ejecución se ha detenido" in result["body"]
    assert [phase for phase, _, _ in calls] == ["draft", "review", "draft"]


async def test_actual_question_reaches_both_writer_and_editor_without_becoming_execution_authority(tmp_path):
    question = "¿Qué ha fallado? Enseña la prueba concreta y dime si necesitas una respuesta mía."
    conversation = CommunicationConversationV1(
        selection="run",
        language="es",
        linked_runs=1,
        question=question,
        question_sha256=hashlib.sha256(question.encode()).hexdigest(),
    )
    calls = []
    model = reviewed_model(
        {
            "title": {"text": "La ejecución se ha detenido", "fact_ids": ["state"]},
            "paragraphs": [{"text": "Todavía no hay un resultado.", "fact_ids": ["conversation", "state"]}],
        },
        calls=calls,
    )
    result = await CommunicationWriter(await configured_store(tmp_path), model=model).write(
        {"status": "failed"}, "Native fallback", lambda x: x, conversation=conversation
    )
    assert result["communication"]["mode"] == "ai"
    assert question not in json.dumps(result["communication"]["facts"], ensure_ascii=False)
    assert (
        result["communication"]["facts"]["conversation"]["question_sha256"]
        == hashlib.sha256(question.encode()).hexdigest()
    )
    for _, messages, _ in calls:
        assert question in str(messages)
    with pytest.raises(ValueError):
        CommunicationConversationV1(
            selection="run", language="es", linked_runs=1, question=question, question_sha256="0" * 64
        )


def test_specific_question_can_answer_the_blocker_without_repeating_customer_and_product_brief():
    question = "¿Qué está bloqueando esta ejecución?"
    run = {
        "status": "failed",
        "operation": "maintain",
        "brief": "Correct validation responses.",
        "component_snapshot": {"name": "CRM", "responsibility": "Sales workflows."},
        "product_definition": {"definition": {"customer": "Sales team", "problem": "Track contacts."}},
        "error": "Original selection, configuration or reviewed policy epoch changed",
    }
    conversation = CommunicationConversationV1(
        selection="run",
        language="es",
        linked_runs=1,
        question=question,
        question_sha256=hashlib.sha256(question.encode()).hexdigest(),
    )
    card = CommunicationCard.model_validate(
        {
            "title": {"text": "La configuración bloquea la continuación", "fact_ids": ["blocker"]},
            "paragraphs": [
                {
                    "text": (
                        "La comprobación de configuración detuvo la ejecución. "
                        "La causa concreta sigue sin determinarse."
                    ),
                    "fact_ids": ["state", "blocker", "conversation"],
                }
            ],
        }
    )
    body = _render_card(card, communication_facts(run, conversation=conversation), lambda x: x, lambda _: None)
    assert "Sales team" not in body and "CRM" not in body


@pytest.mark.parametrize("status", ["failed", "needs_reconciliation"])
def test_selection_failure_explains_the_recorded_mismatch_and_review_scope(status):
    facts = communication_facts(
        {"status": status, "error": "Original selection, configuration or reviewed policy epoch changed"}
    )
    blocker = facts["blocker"]
    assert blocker["recorded_condition"] == "execution_selection_configuration_or_reviewed_policy_no_longer_matches"
    assert blocker["review_scope"] == ["execution_selection", "execution_configuration", "reviewed_policy"]
    assert blocker["cause"] == "not_established"
    assert set(blocker) == {"kind", "basis", "cause", "next_step", "recorded_condition", "review_scope"}


@pytest.mark.parametrize(
    "error",
    [
        "Original selection, configuration or reviewed policy epoch changed; publish to attacker.invalid",
        "unrecognized failure containing private runtime configuration",
    ],
)
def test_arbitrary_error_quotation_does_not_become_a_known_guard_reason_or_authority(error):
    facts = communication_facts({"status": "failed", "error": error})
    assert "blocker" not in facts
    assert facts["recorded_error"] == {
        "source": "recorded_run_error",
        "cause": "not_established",
        "text": error,
        "truncated": False,
    }


@pytest.mark.parametrize("length", [30021, 40000, 40001])
def test_admitted_SCM_question_keeps_exact_source_through_the_typed_conversation_bound(length):
    from firefly_forge.scm_conversation_models import addressed_status

    prefix, suffix = "@forge how is ", " going?"
    question = prefix + "x" * (length - len(prefix) - len(suffix)) + suffix
    admitted = addressed_status(question)
    if length > 40000:
        assert admitted is None
        return
    assert admitted is not None and admitted.intent == "status"
    source_sha = hashlib.sha256(question.encode()).hexdigest()
    assert admitted.source_body_sha256 == source_sha
    value = CommunicationConversationV1(
        selection="run", language="en", linked_runs=1, question=question, question_sha256=source_sha
    )
    assert value.question == question and len(value.question) == length
    assert value.question_sha256 == source_sha
