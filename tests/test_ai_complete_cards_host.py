# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The actual model owns the whole message; native evidence stays in its receipt."""

import json

import pytest
from communication_model_fixture import reviewed_model
from test_communication_writer import configured_store
from test_human_project_updates_host import stopped_run

from firefly_forge.communication import CommunicationWriter, communication_facts
from firefly_forge.community_skills import community_catalog


def full_card(paragraphs, *, title="Execution update", title_facts=None, decision=None):
    return {
        "title": {"text": title, "fact_ids": title_facts or ["state"]},
        "paragraphs": paragraphs,
        "decision": decision,
    }


async def test_ai_writes_the_title_and_entire_card_without_a_second_native_template(tmp_path, monkeypatch):
    import pyfly.agentic.agents as agents

    original = agents.Agent
    calls = []

    def agent(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(agents, "Agent", agent)
    output = full_card(
        [
            {
                "text": "A source-safety check stopped this run. There is no accepted delivery.",
                "fact_ids": ["state", "blocker", "delivery"],
            },
            {
                "text": (
                    "The saved results cover 7 checks, with 1 failure. "
                    "Inspect the workspace and those results before deciding how to continue."
                ),
                "fact_ids": ["check_batch", "blocker"],
            },
        ],
        title="A source-safety check is blocking delivery",
        title_facts=["blocker"],
    )
    writer = CommunicationWriter(await configured_store(tmp_path, "bridge"), model=reviewed_model(output))
    result = await writer.write(stopped_run(), "OLD GENERIC CARD MUST NOT APPEAR", lambda value: value)
    assert result["communication"]["mode"] == "ai"
    assert result["body"].startswith("### A source-safety check is blocking delivery\n")
    assert all(item["text"] in result["body"] for item in output["paragraphs"])
    assert "OLD GENERIC CARD" not in result["body"] and "<details>" not in result["body"]
    humanizer = next(item for item in community_catalog() if item["id"] == "humanizer")
    assert len(calls) == 2 and humanizer["instructions"] in calls[0]["instructions"]
    assert result["communication"]["skills"][0]["adaptation_sha256"] == humanizer["adaptation_sha256"]
    assert calls[1]["name"] == "forge-project-communication-review"


def question_run():
    return {
        "status": "paused",
        "worker_request": {
            "id": "owner",
            "kind": "product_definition",
            "status": "pending",
            "question": "Which team owns this product?",
            "context": "An owner is required before work can continue.",
            "choices": ["Sales", "Support"],
        },
    }


async def test_ai_question_card_preserves_the_question_and_choices_without_appending_old_card(tmp_path):
    run = question_run()
    facts = communication_facts(run)
    assert facts["decision"]["question"] == run["worker_request"]["question"]
    output = full_card(
        [
            {
                "text": "This run is waiting for your product decision before it can continue.",
                "fact_ids": ["state", "decision"],
            }
        ],
        title="Product owner needed",
        title_facts=["decision"],
        decision={
            "question": facts["decision"]["question"],
            "context": facts["decision"]["context"],
            "choices": facts["decision"]["choices"],
        },
    )
    result = await CommunicationWriter(await configured_store(tmp_path, "bridge"), model=reviewed_model(output)).write(
        run, "OLD QUESTION TEMPLATE", lambda value: value
    )
    assert result["communication"]["mode"] == "ai"
    assert all(value in result["body"] for value in [output["decision"]["question"], "Sales", "Support"])
    assert "OLD QUESTION TEMPLATE" not in result["body"] and "<details>" not in result["body"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("question", "Which team has already approved this?"),
        ("choices", ["Sales"]),
        ("context", "The owner has approved this."),
    ],
)
async def test_generated_card_cannot_change_an_authoritative_human_decision(tmp_path, field, value):
    run = question_run()
    decision = {key: run["worker_request"][key] for key in ("question", "context", "choices")}
    decision[field] = value
    output = full_card(
        [{"text": "The run is paused for your decision.", "fact_ids": ["state", "decision"]}], decision=decision
    )
    result = await CommunicationWriter(await configured_store(tmp_path, "bridge"), model=reviewed_model(output)).write(
        run, "Exact native fallback", lambda text: text
    )
    assert result["communication"]["reason"] == "invalid_output" and result["body"] == ""
    assert result["communication"]["mode"] == "held"


@pytest.mark.parametrize("title", ["The run completed", "Everything is deployed", "See @everyone"])
async def test_generated_title_is_subject_to_the_same_fact_and_destination_controls(tmp_path, title):
    output = full_card([{"text": "The run is failed.", "fact_ids": ["state"]}], title=title)
    result = await CommunicationWriter(
        await configured_store(tmp_path),
        model=reviewed_model(
            output,
            verdict="unsupported",
            issues=["unsupported_claim"],
            correction=full_card([{"text": "The execution has a recorded failure.", "fact_ids": ["state"]}]),
        ),
    ).write({"status": "failed"}, "Exact native fallback", lambda value: value)
    assert result["communication"]["reason"] == ("invalid_output" if "@" in title else "generated")
    assert (result["body"] == "") if "@" in title else ("The execution has a recorded failure." in result["body"])


@pytest.mark.parametrize("title", ["Choose Sales", "I recommend Sales", "Sales is the approved owner"])
async def test_question_title_cannot_select_one_of_the_actual_choices(tmp_path, title):
    run = question_run()
    facts = communication_facts(run)
    output = full_card(
        [{"text": "The run is paused for your decision.", "fact_ids": ["state", "decision"]}],
        title=title,
        title_facts=["decision"],
        decision={key: facts["decision"][key] for key in ("question", "context", "choices")},
    )
    result = await CommunicationWriter(
        await configured_store(tmp_path, "bridge"),
        model=reviewed_model(
            output,
            verdict="unsupported",
            issues=["changed_decision"],
            correction=full_card(
                [{"text": "The run remains paused for your decision.", "fact_ids": ["state", "decision"]}],
                title="Your decision is needed",
                title_facts=["decision"],
                decision=output["decision"],
            ),
        ),
    ).write(run, "Exact native fallback", lambda value: value)
    assert result["communication"]["reason"] == "generated" and result["body"].startswith("### Your decision is needed")
    assert result["communication"]["review"]["issues"] == ["changed_decision"]
    assert result["communication"]["repair_attempted"]
    assert result["communication"]["review_applies_to"] == "original_candidate"
    assert result["communication"]["review_candidate_sha256"] != result["communication"]["candidate_sha256"]


async def test_private_question_context_is_refused_before_it_can_enter_a_feedback_receipt(tmp_path):
    store = await configured_store(tmp_path, "bridge")
    run = question_run()
    run["worker_request"]["context"] = "fictional-configured-provider-credential"

    def reject(value):
        if "fictional-configured-provider-credential" in json.dumps(value):
            raise ValueError("Configured credential is not public feedback")

    store.reject_credentials = reject
    writer = CommunicationWriter(store, model=reviewed_model({}))
    with pytest.raises(ValueError, match="Configured credential"):
        await writer.write(run, "Safe native card", lambda value: value)


@pytest.mark.parametrize("punctuated", [False, True])
@pytest.mark.parametrize("title", ["Sales", "Sales owns this product", "Sales's team owns this product"])
async def test_ai_cannot_select_an_owner_by_merely_naming_an_offered_option(tmp_path, title, punctuated):
    run = question_run()
    if punctuated:
        run["worker_request"]["choices"] = ["Sales.", "Support."]
    facts = communication_facts(run)
    output = full_card(
        [{"text": "The run is paused for your decision.", "fact_ids": ["state", "decision"]}],
        title=title,
        title_facts=["decision"],
        decision={key: facts["decision"][key] for key in ("question", "context", "choices")},
    )
    result = await CommunicationWriter(
        await configured_store(tmp_path, "bridge"),
        model=reviewed_model(
            output,
            verdict="unsupported",
            issues=["changed_decision"],
            correction=full_card(
                [{"text": "The run remains paused for your decision.", "fact_ids": ["state", "decision"]}],
                title="Your decision is needed",
                title_facts=["decision"],
                decision=output["decision"],
            ),
        ),
    ).write(run, "Exact native fallback", lambda value: value)
    assert result["body"].startswith("### Your decision is needed") and result["communication"]["reason"] == "generated"
    assert result["communication"]["review"]["verdict"] == "unsupported"
    assert result["communication"]["repair_attempted"]
    assert result["communication"]["review_applies_to"] == "original_candidate"
    assert result["communication"]["review_candidate_sha256"] != result["communication"]["candidate_sha256"]
