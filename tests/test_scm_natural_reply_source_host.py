# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Unmentioned replies need exact durable question provenance, not a fabricated address."""

import copy
import hashlib
import importlib
import importlib.util

import pytest


def question():
    return {
        "origin": "product_assistant_turn",
        "project_id": "a" * 32,
        "repository_id": "b" * 32,
        "native_user_id": "2" * 32,
        "actor": "7",
        "issue": {"kind": "issue", "number": 4},
        "binding_digest": "c" * 64,
        "source_receipt_id": "d" * 32,
        "question_id": "product-" + "e" * 32,
        "question_digest": "f" * 64,
        "conversation_id": "0" * 32,
        "turn_id": "e" * 32,
        "publication_comment_id": "100",
        "publication_body_sha256": "1" * 64,
        "questions": ["Who reviews this?", "Who may reassign it?"],
        "pending": True,
    }


def implementation():
    assert importlib.util.find_spec("firefly_forge.scm_natural_replies"), (
        "Unmentioned question-reply provenance is missing"
    )
    return importlib.import_module("firefly_forge.scm_natural_replies")


def bind(body, rows):
    return implementation().retained_reply(
        body,
        rows,
        project="a" * 32,
        repository="b" * 32,
        actor="7",
        issue={"kind": "issue", "number": 4},
        binding_digest="c" * 64,
        native_user_id="2" * 32,
    )


def test_unmentioned_reply_retains_real_source_kind_and_exact_attachment_reference_text():
    body = "Ventas revisa. Adjunto [ejemplo](https://public.example.test/example.png)."
    result = bind(body, [question()])
    assert result.source_kind == "retained_question_reply" and result.adapter != "addressed_status_v1"
    assert result.source_body_sha256 == hashlib.sha256(body.encode()).hexdigest()
    assert result.message == body and len(result.question_refs) == 1


@pytest.mark.parametrize("change", ["actor", "issue", "binding_digest", "repository_id", "native_user_id", "consumed"])
def test_unmentioned_comment_cannot_consume_another_owner_thread_or_consumed_question(change):
    row = question()
    if change == "issue":
        row["issue"] = {"kind": "issue", "number": 9}
    elif change == "consumed":
        row["pending"] = False
    else:
        row[change] = "9" * (64 if change == "binding_digest" else 32)
    assert bind("Natural reply", [row]) is None


def test_parallel_question_sources_are_retained_for_clarification_without_newest_selection():
    first = question()
    other = copy.deepcopy(first)
    other.update(
        question_digest="9" * 64, source_receipt_id="8" * 32, turn_id="7" * 32, question_id="product-" + "7" * 32
    )
    result = bind("Yes", [other, first])
    assert len(result.question_refs) == 2
    assert result.intent == "clarify" and not hasattr(result, "run_id")


def test_empty_or_prose_only_context_does_not_create_a_question_reply_source():
    assert bind("Ordinary unaddressed discussion", []) is None
    row = question()
    row["questions"] = []
    with pytest.raises(ValueError):
        bind("Yes", [row])
