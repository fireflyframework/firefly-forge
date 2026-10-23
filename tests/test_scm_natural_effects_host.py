# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Derived task commands retain the original comment and cannot expand its question scope."""

import copy
import importlib
import importlib.util

import pytest


def implementation():
    assert importlib.util.find_spec("firefly_forge.scm_natural_effects"), "Natural effect admission is missing"
    return importlib.import_module("firefly_forge.scm_natural_effects")


def parent():
    return {
        "id": "a" * 32,
        "status": "processed",
        "repository_id": "b" * 32,
        "provider": "github",
        "remote_id": "42",
        "actor": "7",
        "digest": "c" * 64,
        "issue": {"kind": "issue", "number": 3},
        "verification": {"resource_id": "91"},
        "source_snapshot": {"comment_id": "91", "body": "@forge Sales"},
        "natural_turn": {
            "state": "answer_pending_permission",
            "resolution": {
                "state": "answer",
                "run_id": "d" * 32,
                "question_id": "review-owner",
                "question_digest": "e" * 64,
                "answer": "Sales",
                "response_digest": None,
            },
        },
    }


def test_answer_command_uses_only_original_human_text_and_server_resolved_question():
    module = implementation()
    original = parent()
    command = module.answer_command(original)
    assert command == {"action": "respond", "run_id": "d" * 32, "request_id": "review-owner", "answer": "Sales"}
    assert original == parent()


@pytest.mark.parametrize("change", ["actor", "issue", "repository_id", "digest", "command", "parent"])
def test_projected_child_cannot_change_verified_source_or_approved_command(change):
    module = implementation()
    original = parent()
    child = module.projected_delivery(original, module.answer_command(original), stage="answer", admission="f" * 32)
    original["natural_effects"] = {child["id"]: copy.deepcopy(child["natural_parent"])}
    assert module.validate_projection(child, original) is None
    if change == "parent":
        original["natural_effects"] = {}
    elif change == "issue":
        child["issue"] = {"kind": "issue", "number": 4}
    elif change == "command":
        child["command"]["run_id"] = "9" * 32
    else:
        child[change] = "9" * (64 if change == "digest" else 32)
    with pytest.raises(ValueError):
        module.validate_projection(child, original)


def test_child_identity_is_stable_for_one_admission_and_distinct_for_explicit_readmission():
    module = implementation()
    original = parent()
    command = module.answer_command(original)
    first = module.projected_delivery(original, command, stage="answer", admission="f" * 32)
    assert first == module.projected_delivery(original, command, stage="answer", admission="f" * 32)
    second = module.projected_delivery(original, command, stage="answer", admission="0" * 32)
    assert first["id"] != second["id"]
    assert first["source_snapshot"] == original["source_snapshot"] == second["source_snapshot"]
    assert first["verification"] == original["verification"]


def test_stale_question_or_rewritten_answer_cannot_be_readmitted():
    module = implementation()
    original = parent()
    current = {
        "run_id": "d" * 32,
        "question_id": "review-owner",
        "question_digest": "e" * 64,
        "question_status": "pending",
        "run_status": "paused",
        "consumed": False,
    }
    module.validate_answer(original, current)
    for field, value in [
        ("question_digest", "0" * 64),
        ("question_id", "different"),
        ("consumed", True),
        ("question_status", "answered"),
    ]:
        changed = {**current, field: value}
        with pytest.raises(ValueError):
            module.validate_answer(original, changed)
