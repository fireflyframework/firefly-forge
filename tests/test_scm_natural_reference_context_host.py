# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Natural answers preserve repository-bound facts from published product questions."""

import importlib

import pytest
from test_scm_natural_product_questions_host import fixture

from firefly_forge.communication import assistant_communication, communication_facts


def referenced_question():
    receipt, session, turn, events = fixture()
    receipt["command"]["operation"] = "turn"
    receipt["action_result"] = {
        "conversation_id": session["id"],
        "turn_id": turn["id"],
        "status": "completed",
        "reply": turn["reply"]["message"],
        "questions": turn["reply"]["questions"],
        "proposal_reviewed": False,
        "proposal_status": "unreviewed",
        "remote_reference": {
            "published": True,
            "url": "https://github.com/team/product/tree/forge/references/" + "d" * 64,
            "digest": "d" * 64,
        },
    }
    repository = {"url": "https://github.com/team/product.git"}
    assistant = assistant_communication(receipt, repository=repository, host="https://github.com")
    assert assistant.reply_reference_digest
    receipt["feedback_intent"]["communication"]["facts"] = communication_facts(None, assistant=assistant)
    return receipt, session, turn, events, repository


def test_published_product_question_retains_canonical_reference_context():
    receipt, session, turn, events, repository = referenced_question()
    module = importlib.import_module("firefly_forge.scm_natural_questions")
    result = module.product_questions(receipt, session, turn, events, repository=repository, host="https://github.com")
    assert result["turn_id"] == turn["id"] and result["questions"] == turn["reply"]["questions"]


@pytest.mark.parametrize(
    "host,repository",
    [
        ("https://gitlab.com", {"url": "https://github.com/team/product.git"}),
        ("https://github.com", {"url": "https://github.com/team/other.git"}),
    ],
)
def test_published_product_question_rejects_different_reference_context(host, repository):
    receipt, session, turn, events, _ = referenced_question()
    module = importlib.import_module("firefly_forge.scm_natural_questions")
    with pytest.raises(ValueError, match="typed producer facts"):
        module.product_questions(receipt, session, turn, events, repository=repository, host=host)
