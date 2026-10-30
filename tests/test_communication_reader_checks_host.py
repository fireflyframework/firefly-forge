# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Editorial checks independently govern the existing single-correction path."""

import json

import pytest
from communication_model_fixture import communication_phase
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from test_communication_writer import configured_store

from firefly_forge.communication import CommunicationWriter, communication_facts

CHECKS = {
    "answers_question": True,
    "relevant_detail": True,
    "no_redundancy": True,
    "plain_language": True,
    "useful_excerpts": True,
}


async def reply_with_review(tmp_path, checks, *, issues=()):
    reviewed = False

    def model(messages, info):
        nonlocal reviewed
        if communication_phase(info) == "review":
            reviewed = True
            value = {
                "verdict": "supported",
                "issues": list(issues),
                "fact_ids": ["state"],
                "rationale": "The facts are supported; the reader checks assess the wording separately.",
            }
            if checks is not None:
                value["reader_checks"] = checks
        else:
            value = {
                "title": {"text": "Run stopped", "fact_ids": ["state"]},
                "paragraphs": [
                    {
                        "text": "The run needs attention." if reviewed else "The selected run has a failed status.",
                        "fact_ids": ["state"],
                    }
                ],
            }
        return ModelResponse(parts=[TextPart(json.dumps(value))])

    return await CommunicationWriter(await configured_store(tmp_path, "bridge"), model=FunctionModel(model)).write(
        {"status": "failed"}, "Native fallback", lambda x: x
    )


@pytest.mark.parametrize("check", list(CHECKS))
async def test_factual_support_with_a_failed_reader_check_gets_one_ai_correction(tmp_path, check):
    checks = {**CHECKS, check: False}
    result = await reply_with_review(tmp_path, checks)
    evidence = result["communication"]
    assert evidence["mode"] == "ai"
    assert evidence["repair_attempted"] is True
    assert evidence["review"]["verdict"] == "supported"
    assert evidence["review"]["reader_checks"] == checks
    assert evidence["review_applies_to"] == "original_candidate"
    assert evidence["review_candidate_sha256"] != evidence["candidate_sha256"]
    assert "The run needs attention." in result["body"]
    assert "Native fallback" not in result["body"]


@pytest.mark.parametrize("issue", ["poor_structure", "unclear_language"])
async def test_supported_facts_with_editorial_issue_use_correction_instead_of_invalid_review(tmp_path, issue):
    result = await reply_with_review(tmp_path, {**CHECKS, "plain_language": False}, issues=[issue])
    assert result["communication"]["mode"] == "ai"
    assert result["communication"]["repair_attempted"] is True
    assert result["communication"]["review"]["issues"] == [issue]


@pytest.mark.parametrize(
    "checks",
    [None, {}, {**CHECKS, "plain_language": "true"}, {**CHECKS, "no_redundancy": 1}, {**CHECKS, "extra": True}],
)
async def test_missing_or_non_boolean_reader_checks_hold_without_a_default_pass(tmp_path, checks):
    result = await reply_with_review(tmp_path, checks)
    assert result["communication"]["mode"] == "held"
    assert result["communication"]["reason"] == "invalid_review"
    assert result["communication"]["repair_attempted"] is False
    assert result["body"] == ""


def test_successful_stdout_is_not_offered_as_a_public_status_excerpt_but_the_command_remains():
    run = {
        "status": "failed",
        "attempt": 2,
        "final_verification": [
            {"attempt": 1, "exit_code": 0, "argv": ["python", "-m", "pytest"], "output": "1 passed"},
            {"attempt": 2, "exit_code": 1, "argv": ["python", "-m", "pytest"], "output": "E assert 500 == 422"},
        ],
    }
    diagnostics = communication_facts(run)["check_diagnostics"]
    assert "check-1-command" in diagnostics
    assert "check-1-output" not in diagnostics
    assert diagnostics["check-2-output"]["text"] == "E assert 500 == 422"
    assert run["final_verification"][0]["output"] == "1 passed"
