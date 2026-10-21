# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Retain grounded delivery denials without admitting positive completion claims."""

import pytest
from communication_model_fixture import reviewed_model
from test_communication_writer import configured_store
from test_human_project_updates_host import stopped_run

from firefly_forge.communication import CommunicationWriter


async def test_actual_codex_delivery_denial_is_not_mistaken_for_a_completion_claim(tmp_path):
    store = await configured_store(tmp_path, "bridge")
    draft = {
        "title": {"text": "Execution update", "fact_ids": ["state"]},
        "paragraphs": [
            {
                "text": (
                    "The recorded run failed because the source-safety guard stopped admission. "
                    "This run was not accepted as a completed delivery, and no review link is recorded here. "
                    "The guard result does not establish credential exposure."
                ),
                "fact_ids": ["state", "blocker", "delivery"],
            },
            {
                "text": (
                    "The retained governed check batch records 7 of 7 checks, with 1 failing. "
                    "These retained results do not verify the current attempt. The supported next step is to "
                    "inspect the workspace and recorded checks; the record does not authorize another execution."
                ),
                "fact_ids": ["check_batch", "blocker"],
            },
        ],
    }
    result = await CommunicationWriter(store, model=reviewed_model(draft)).write(
        stopped_run(), "Exact native evidence", lambda value: value
    )
    assert result["communication"]["mode"] == "ai"
    assert all(paragraph["text"] in result["body"] for paragraph in draft["paragraphs"])
    assert "Exact native evidence" not in result["body"]


@pytest.mark.parametrize(
    "denial",
    [
        "This is not a completed delivery.",
        "This run was not accepted as a completed delivery.",
        "This native run was not accepted as a completed delivery.",
        "The recorded run was not accepted as a completed delivery.",
    ],
)
async def test_delivery_denial_requires_supported_review_of_its_native_facts(tmp_path, denial):
    draft = {
        "title": {"text": "Execution update", "fact_ids": ["state"]},
        "paragraphs": [{"text": denial, "fact_ids": ["state", "blocker", "check_batch", "delivery"]}],
    }
    result = await CommunicationWriter(await configured_store(tmp_path), model=reviewed_model(draft)).write(
        stopped_run(), "Exact native fallback", lambda value: value
    )
    assert result["communication"]["mode"] == "ai" and denial in result["body"]
    draft["paragraphs"][0]["fact_ids"].remove("delivery")
    calls = []
    rejected = await CommunicationWriter(
        await configured_store(tmp_path),
        model=reviewed_model(
            draft,
            verdict="unsupported",
            issues=["insufficient_context"],
            calls=calls,
            correction={
                "title": {"text": "Execution update", "fact_ids": ["state"]},
                "paragraphs": [
                    {
                        "text": "There is no accepted delivery. Saved checks include a failure.",
                        "fact_ids": ["state", "blocker", "check_batch", "delivery"],
                    }
                ],
            },
        ),
    ).write(stopped_run(), "Exact native fallback", lambda value: value)
    assert rejected["communication"]["reason"] == "generated"
    assert rejected["communication"]["review"]["issues"] == ["insufficient_context"]
    assert "There is no accepted delivery." in rejected["body"] and [phase for phase, _, _ in calls] == [
        "draft",
        "review",
        "draft",
    ]


@pytest.mark.parametrize(
    "claim",
    [
        "This is not a completed delivery. The run completed.",
        "The run succeeded, although this was not accepted as a completed delivery.",
        "This is not only a completed delivery.",
        "The run is finished. This run was not accepted as a completed delivery.",
        "It is false that this run was not accepted as a completed delivery.",
        "This run was not accepted as a completed delivery is false.",
    ],
)
async def test_a_delivery_denial_does_not_override_an_unsupported_semantic_review(tmp_path, claim):
    draft = {
        "title": {"text": "Execution update", "fact_ids": ["state"]},
        "paragraphs": [{"text": claim, "fact_ids": ["state", "blocker", "check_batch", "delivery"]}],
    }
    calls = []
    result = await CommunicationWriter(
        await configured_store(tmp_path),
        model=reviewed_model(
            draft,
            verdict="unsupported",
            issues=["unsupported_claim"],
            calls=calls,
            correction={
                "title": {"text": "Execution update", "fact_ids": ["state"]},
                "paragraphs": [
                    {
                        "text": "There is no accepted delivery. Saved checks include a failure.",
                        "fact_ids": ["state", "blocker", "check_batch", "delivery"],
                    }
                ],
            },
        ),
    ).write(stopped_run(), "Exact native fallback", lambda value: value)
    assert result["communication"]["reason"] == "generated" and "There is no accepted delivery." in result["body"]
    assert result["communication"]["review"]["verdict"] == "unsupported"
    assert result["communication"]["review_applies_to"] == "original_candidate"
    assert result["communication"]["review_candidate_sha256"] != result["communication"]["candidate_sha256"]
    assert [phase for phase, _, _ in calls] == ["draft", "review", "draft"]
