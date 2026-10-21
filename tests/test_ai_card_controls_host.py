# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled question quotations and server-owned references require no provider access."""

import pytest
from test_communication_writer import configured_store

from firefly_forge.communication import CommunicationCard, CommunicationWriter, _validate_draft, communication_facts


def pending_request(**changes):
    request = {
        "id": "owner",
        "kind": "product_definition",
        "status": "pending",
        "question": "Which team owns this product?",
        "context": "A product owner is required.",
        "choices": ["Sales", "Support"],
        **changes,
    }
    return {"id": "a" * 32, "status": "paused", "worker_request": request}


@pytest.mark.parametrize(
    "changes",
    [
        {"question": "q" * 601},
        {"context": "c" * 601},
        {"choices": ["x" * 160 + "A", "x" * 160 + "B"]},
        {"choices": ["Sales", "Sales"]},
        {"question": "." * 301},
    ],
)
async def test_incomplete_or_collapsed_question_quotes_fall_back_before_model_creation(tmp_path, monkeypatch, changes):
    import fireflyframework_agentic.agents as agents

    def forbidden(**kwargs):
        pytest.fail("Unsupported human quotations must not construct a model agent")

    monkeypatch.setattr(agents, "FireflyAgent", forbidden)
    result = await CommunicationWriter(await configured_store(tmp_path)).write(
        pending_request(**changes), "Exact native question", lambda value: value
    )
    assert result["body"] == "Exact native question"
    assert result["communication"]["reason"] == "decision_too_large"


def test_punctuated_choice_cannot_bypass_the_conservative_human_selection_guard():
    facts = communication_facts(pending_request(choices=["Sales.", "Support."]))
    card = CommunicationCard(
        title={"text": "Choose Sales", "fact_ids": ["decision"]},
        paragraphs=[{"text": "The run is paused for your decision.", "fact_ids": ["state", "decision"]}],
        decision={key: facts["decision"][key] for key in ("question", "context", "choices")},
    )
    with pytest.raises(ValueError, match="(?i)human"):
        _validate_draft(card, facts)


def test_reference_slot_preserves_exact_scoped_links_and_never_invents_channel_actions():
    from firefly_forge.collaboration_feedback import communication_controls

    data = {"id": "b" * 32, "scm_execution_actor": {"scm_delegation": {"action": "status"}}}
    run = {**pending_request(), "pr_url": "https://github.com/team/product/pull/7"}
    repository = {"url": "https://github.com/team/product.git"}
    body = communication_controls(data, run, repository, "https://github.com", lambda value: value)
    assert "a" * 32 in body and "b" * 32 in body
    assert "https://github.com/team/product/pull/7" in body
    assert "project operator" in body and "stays paused" in body
    assert "/forge respond" not in body and "/forge resume" not in body
    assert "Which team" not in body and "Sales" not in body


def test_reference_slot_rejects_unscoped_destinations_and_non_native_identifiers():
    from firefly_forge.collaboration_feedback import communication_controls

    body = communication_controls(
        {
            "id": "invented",
            "action_result": {"remote_reference": {"published": True, "url": "https://attacker.test/ref"}},
        },
        {"id": "@everyone", "status": "failed", "pr_url": "https://github.com/other/product/pull/7"},
        {"url": "https://github.com/team/product.git"},
        "https://github.com",
        lambda value: value,
    )
    assert body == ""
