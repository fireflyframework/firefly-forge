# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Retain grounded delivery denials without admitting positive completion claims."""

import json

import pytest
from pydantic_ai.models.test import TestModel
from test_communication_writer import configured_store
from test_human_project_updates_host import stopped_run

from firefly_forge.communication import CommunicationDraft, CommunicationWriter, _validate_draft, communication_facts


async def test_actual_codex_delivery_denial_is_not_mistaken_for_a_completion_claim(tmp_path):
    store = await configured_store(tmp_path, "bridge")
    draft = {
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
        ]
    }
    result = await CommunicationWriter(store, model=TestModel(custom_output_text=json.dumps(draft))).write(
        stopped_run(), "Exact native evidence", lambda value: value
    )
    assert result["communication"]["mode"] == "ai"
    visible, record = result["body"].split("<details>", 1)
    assert all(paragraph["text"] in visible for paragraph in draft["paragraphs"])
    assert "Exact native evidence" in record


@pytest.mark.parametrize(
    "denial", ["This is not a completed delivery.", "This run was not accepted as a completed delivery."]
)
def test_delivery_denial_requires_the_cited_native_nonacceptance_fact(denial):
    facts = communication_facts(stopped_run())
    paragraph = {"text": denial, "fact_ids": ["state", "blocker", "check_batch", "delivery"]}
    assert _validate_draft(CommunicationDraft.model_validate({"paragraphs": [paragraph]}), facts) == denial
    paragraph["fact_ids"].remove("delivery")
    with pytest.raises(ValueError, match="completion"):
        _validate_draft(CommunicationDraft.model_validate({"paragraphs": [paragraph]}), facts)


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
def test_a_delivery_denial_does_not_excuse_an_additional_completion_claim(claim):
    facts = communication_facts(stopped_run())
    draft = CommunicationDraft.model_validate(
        {"paragraphs": [{"text": claim, "fact_ids": ["state", "blocker", "check_batch", "delivery"]}]}
    )
    with pytest.raises(ValueError, match="completion"):
        _validate_draft(draft, facts)
