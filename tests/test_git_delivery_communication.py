# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Intermediate PR wording never borrows final-success authority."""

import pytest

from firefly_forge import communication as c


def test_checkpoint_facts_explicitly_keep_verification_pending():
    model = getattr(c, "CommunicationGitDeliveryV1", None)
    assert model is not None, "Intermediate checkpoint facts are missing"
    facts = c.communication_facts(
        {"status": "paused"},
        git_delivery=model(checkpoint_revision="a" * 40, source_tree="b" * 40, sequence=2, changed_file_count=3),
    )
    assert facts["git_delivery"]["verification"] == "pending"
    assert facts["git_delivery"]["purpose"] == "intermediate_draft_pull_request"
    assert "publication" not in facts and facts["state"]["status"] == "paused"


def test_intermediate_facts_cannot_be_combined_with_final_publication():
    model = getattr(c, "CommunicationGitDeliveryV1", None)
    assert model is not None, "Intermediate checkpoint facts are missing"
    with pytest.raises(ValueError):
        c.communication_facts(
            {"status": "succeeded"},
            git_delivery=model(checkpoint_revision="a" * 40, source_tree="b" * 40, sequence=1, changed_file_count=1),
            publication=c.CommunicationPublicationV1(
                summary="Public fixture", source_tree="b" * 40, checkpoint_revision="a" * 40
            ),
        )


def test_status_retains_exact_checkpoint_reference_without_claiming_final_readiness():
    facts = c.communication_facts(
        {
            "status": "paused",
            "git_delivery_summary": {
                "revision": "a" * 40,
                "source_tree": "b" * 40,
                "sequence": 2,
                "remote_status": "draft",
                "pr_url": "https://example.test/team/product/pull/7",
            },
        }
    )
    assert facts["git_checkpoint"] == {
        "revision": "a" * 40,
        "source_tree": "b" * 40,
        "sequence": 2,
        "remote_status": "draft",
    }
    assert "publication" not in facts and "delivery" not in facts
    card = c.CommunicationCard(
        title={"text": "An implementation checkpoint is retained", "fact_ids": ["git_checkpoint"]},
        paragraphs=[
            {
                "text": "The run is paused and the remote reference remains a draft.",
                "fact_ids": ["state", "git_checkpoint"],
            }
        ],
    )
    body = c._render_card(card, facts, lambda text: text, lambda value: None)
    assert "a" * 40 in body and "b" * 40 in body


def test_malformed_checkpoint_summary_cannot_become_model_facts():
    facts = c.communication_facts(
        {
            "status": "paused",
            "git_delivery_summary": {
                "revision": "private raw value",
                "source_tree": "b" * 40,
                "sequence": True,
                "remote_status": "ready",
            },
        }
    )
    assert "git_checkpoint" not in facts
