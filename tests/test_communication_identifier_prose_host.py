# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Recorded test identifiers remain readable without acquiring Markdown authority."""

import json

import pytest
from communication_model_fixture import communication_phase
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from test_communication_writer import configured_store

from firefly_forge.communication import CommunicationCard, CommunicationWriter, _render_card, communication_facts


@pytest.mark.parametrize(
    "identifier", ["test_csrf_and_durable_relationships", "__init__", "_private_name", "part__name"]
)
def test_identifier_prose_is_rendered_as_literal_markdown_text(identifier):
    card = CommunicationCard.model_validate(
        {
            "title": {"text": identifier, "fact_ids": ["state"]},
            "paragraphs": [{"text": "Inspect " + identifier, "heading": identifier, "fact_ids": ["state"]}],
        }
    )
    body = _render_card(card, {"state": {"status": "failed"}}, lambda x: x, lambda _: None)
    assert body.count(identifier.replace("_", "\\_")) == 3


@pytest.mark.parametrize(
    "value", ["@forge", "`code`", "*emphasis*", "<script>", "https://example.invalid", "[link](target)"]
)
def test_other_markdown_or_destination_syntax_remains_rejected(value):
    card = CommunicationCard.model_validate(
        {
            "title": {"text": "Failure", "fact_ids": ["state"]},
            "paragraphs": [{"text": value, "fact_ids": ["state"]}],
        }
    )
    with pytest.raises(ValueError, match="plain"):
        _render_card(card, {"state": {"status": "failed"}}, lambda x: x, lambda _: None)


async def test_one_editorial_repair_can_name_the_failed_test_without_becoming_a_held_reply(tmp_path):
    calls = []

    def model(messages, info):
        phase = communication_phase(info)
        calls.append(phase)
        if phase == "review":
            value = {
                "verdict": "supported",
                "issues": ["insufficient_context"],
                "fact_ids": ["state"],
                "rationale": "Name the recorded failing test.",
                "reader_checks": {
                    "answers_question": False,
                    "relevant_detail": True,
                    "no_redundancy": True,
                    "plain_language": True,
                    "useful_excerpts": True,
                },
            }
        else:
            text = "The test failed." if len(calls) == 1 else "The failing test is test_csrf_and_durable_relationships."
            value = {
                "title": {"text": "Validation failed", "fact_ids": ["state"]},
                "paragraphs": [{"text": text, "fact_ids": ["state"]}],
                "decision": None,
            }
        return ModelResponse(parts=[TextPart(json.dumps(value))])

    result = await CommunicationWriter(await configured_store(tmp_path, "bridge"), model=FunctionModel(model)).write(
        {"status": "failed"}, "Unused native fallback", lambda x: x
    )
    assert len(calls) == 3
    assert result["communication"]["mode"] == "ai"
    assert result["communication"]["repair_attempted"] is True
    assert "test\\_csrf\\_and\\_durable\\_relationships" in result["body"]
    assert "Unused native fallback" not in result["body"]


@pytest.mark.parametrize("status", ["failed", "needs_reconciliation"])
def test_recorded_worker_identity_guard_exposes_the_failed_step_without_inventing_its_cause(status):
    run = {"status": status, "error": "Retained verified source requires its current authenticated worker"}
    before = dict(run)
    blocker = communication_facts(run)["blocker"]
    assert blocker["kind"] == "worker_identity_guard"
    assert blocker["phase"] == "retained_verification_result_admission"
    assert blocker["cause"] == "not_established"
    assert "execution_grant" not in blocker and run == before


def test_old_worker_identity_error_does_not_turn_an_active_run_into_a_failed_one():
    assert "blocker" not in communication_facts(
        {"status": "developing", "error": "Retained verified source requires its current authenticated worker"}
    )
