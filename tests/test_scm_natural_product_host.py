# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Product handoffs cannot manufacture a session, message, allowance or human source."""

import copy
import importlib

import pytest

from firefly_forge.project_access import AccessDenied


def module():
    return importlib.import_module("firefly_forge.scm_natural_product")


def parent(*, answer=True):
    return {
        "id": "a" * 32,
        "status": "processed",
        "repository_id": "b" * 32,
        "provider": "github",
        "remote_id": "42",
        "actor": "7",
        "digest": "c" * 64,
        "issue": {"kind": "issue", "number": 3},
        "source_snapshot": {"body": "Use the warehouse owners.", "comment_id": "102"},
        "verification": {"resource_id": "102"},
        "natural_turn": {
            "classifier_outcome": "confirmed_valid",
            "state": "product_answer" if answer else "product_request",
            "human_message": "Use the warehouse owners.",
            "resolution": {
                "state": "product_answer" if answer else "product_request",
                "scope": {"project_id": "d" * 32},
                "answer": "Use the warehouse owners.",
                "question_id": "product-" + "e" * 32 if answer else None,
                "question_digest": "f" * 64 if answer else None,
                "turn_id": "e" * 32 if answer else None,
                "conversation_id": "1" * 32 if answer else None,
            },
        },
    }


def projected(*, answer=True):
    original = parent(answer=answer)
    cut = {
        "grant_digest": "2" * 64,
        "binding_digest": "3" * 64,
        "scopes": [{"native_user_id": "4" * 32}],
        "conversation_id": "1" * 32 if answer else None,
    }
    child = module().projected_product(original, cut)
    original["natural_product_effects"] = {child["id"]: copy.deepcopy(child["natural_product_parent"])}
    return original, child


def test_product_answer_projects_exact_original_session_and_original_words():
    original, child = projected()
    module().validate_product_projection(child, original)
    assert child["command"]["operation"] == "turn"
    assert child["command"]["conversation_id"] == "1" * 32
    assert child["command"]["message"] == original["natural_turn"]["human_message"]
    assert "run_id" not in child["command"] and "approve" not in child["command"]


def test_new_product_request_starts_native_session_without_a_model_supplied_identifier():
    original, child = projected(answer=False)
    module().validate_product_projection(child, original)
    assert child["command"] == {"action": "assistant", "operation": "start", "project_id": "d" * 32}
    turn = module().product_command(original, "5" * 32)
    assert turn["operation"] == "turn" and turn["message"] == original["natural_turn"]["human_message"]


@pytest.mark.parametrize(
    "change", ["message", "conversation", "grant", "source", "mapping", "custody", "unknown", "approval"]
)
def test_changed_product_projection_never_reaches_native_functional_authority(change):
    original, child = projected()
    if change == "message":
        child["command"]["message"] = "The model changed the person's answer"
    elif change == "conversation":
        child["command"]["conversation_id"] = "9" * 32
    elif change == "grant":
        child["natural_product_parent"]["grant_digest"] = "9" * 64
    elif change == "source":
        child["source_snapshot"]["body"] += " changed"
    elif change == "mapping":
        child["actor"] = "8"
    elif change == "custody":
        original["natural_product_effects"] = {}
    elif change == "unknown":
        original["natural_turn"]["state"] = "needs_reconciliation"
    else:
        child["command"] = {"action": "approve", "run_id": "9" * 32}
    with pytest.raises((AccessDenied, ValueError, KeyError)):
        module().validate_product_projection(child, original)
