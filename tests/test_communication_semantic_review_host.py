# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Structured AI review owns meaning; native controls own authority and framing."""

import json

import pytest
from communication_model_fixture import communication_phase
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from test_communication_writer import configured_store
from test_human_project_updates_host import stopped_run

from firefly_forge.communication import CommunicationWriter


def draft(text):
    return {
        "title": {"text": "Source-safety check stopped admission", "fact_ids": ["blocker"]},
        "paragraphs": [
            {"text": text, "fact_ids": ["state", "blocker", "delivery"]},
            {
                "text": "Saved results cover 7 checks, with 1 failure. Inspect the workspace and saved results.",
                "fact_ids": ["check_batch", "blocker"],
            },
        ],
        "decision": None,
    }


def reviewed_model(value, verdict="supported", issues=None, *, review_override=None):
    calls = []
    review = {
        "verdict": verdict,
        "issues": issues or [],
        "fact_ids": ["state", "blocker", "delivery", "check_batch"],
        "rationale": "The candidate matches the supplied facts.",
    }
    if review_override:
        review.update(review_override)

    def model(messages, info):
        phase = communication_phase(info)
        calls.append({"messages": messages, "info": info, "phase": phase})
        if phase == "review":
            response = review
        elif sum(item["phase"] == "draft" for item in calls) == 1:
            response = value
        else:
            response = draft("A source-safety check stopped this run. There is no accepted delivery.")
        return ModelResponse(parts=[TextPart(json.dumps(response))])

    return FunctionModel(model), calls


@pytest.mark.parametrize(
    "text",
    [
        "The safety check does not establish that credentials were exposed. There is no accepted delivery.",
        "This run was not accepted as a completed delivery. The saved checks do not establish a credential leak.",
    ],
)
async def test_semantic_reviewer_accepts_grounded_negation_without_keyword_rejection(tmp_path, text):
    model, calls = reviewed_model(draft(text))
    result = await CommunicationWriter(await configured_store(tmp_path, "bridge"), model=model).write(
        stopped_run(),
        "Native fallback",
        lambda value: value,
    )
    assert len(calls) == 2
    assert result["communication"]["mode"] == "ai"
    assert result["communication"]["review"]["verdict"] == "supported"
    assert text in result["body"] and "Native fallback" not in result["body"]
    assert calls[0]["info"].function_tools == [] and calls[1]["info"].function_tools == []


@pytest.mark.parametrize(
    "verdict,issues,reason",
    [
        ("unsupported", ["unsupported_claim"], "semantic_rejected"),
        ("uncertain", ["uncertain"], "semantic_uncertain"),
    ],
)
async def test_semantic_refusal_or_uncertainty_gets_one_final_ai_correction_and_response(
    tmp_path, verdict, issues, reason
):
    model, calls = reviewed_model(draft("Credentials were exposed. Everything is complete."), verdict, issues)
    result = await CommunicationWriter(await configured_store(tmp_path, "bridge"), model=model).write(
        stopped_run(),
        "Exact native fallback",
        lambda value: value,
    )
    assert len(calls) == 3
    assert result["communication"]["mode"] == "ai"
    assert result["communication"]["repair_attempted"] is True
    assert result["communication"]["review"]["verdict"] == verdict
    assert result["communication"]["review_applies_to"] == "original_candidate"
    assert result["communication"]["review_candidate_sha256"] != result["communication"]["candidate_sha256"]
    assert "Credentials were exposed" not in result["body"]
    assert "There is no accepted delivery" in result["body"]


@pytest.mark.parametrize(
    "override",
    [
        {"verdict": "supported", "issues": ["unsupported_claim"]},
        {"fact_ids": ["unknown"]},
        {"fact_ids": ["state", "state"]},
        {"fact_ids": ["state"]},
    ],
)
async def test_malformed_or_unbound_review_cannot_authorize_a_message(tmp_path, override):
    model, calls = reviewed_model(draft("A source-safety check stopped admission."), review_override=override)
    result = await CommunicationWriter(await configured_store(tmp_path, "bridge"), model=model).write(
        stopped_run(),
        "Exact native fallback",
        lambda value: value,
    )
    assert len(calls) == 2 and result["body"] == ""
    assert result["communication"]["mode"] == "held"
    assert result["communication"]["reason"] == "invalid_review"
