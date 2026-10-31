# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Older drafts and orphan receipts cannot manufacture current PR confirmation."""

import copy

import pytest

from firefly_forge.github_projects_references import checkpoint_pull_requests


def evidence():
    checkpoint = {"request_id": "1" * 32, "revision": "a" * 40, "checkpoint_digest": "b" * 64}
    intent = {
        "kind": "git_delivery_remote_intent",
        "idempotency_key": "draft-one",
        "sequence": 1,
        "digest": "c" * 64,
        "payload": {**checkpoint, "repository_id": "2" * 32, "branch": "forge/one", "body_sha256": "d" * 64},
    }
    receipt = {
        "kind": "git_delivery_remote_receipt",
        "idempotency_key": "draft-one",
        "sequence": 2,
        "payload": {
            "request_id": checkpoint["request_id"],
            "revision": checkpoint["revision"],
            "intent_digest": intent["digest"],
            "remote_status": "draft",
            "reference": {"url": "https://github.com/team/product/pull/7", "body_sha256": "d" * 64},
        },
    }
    return checkpoint, intent, receipt


def test_exact_confirmed_checkpoint_retains_its_pr_reference():
    checkpoint, intent, receipt = evidence()
    assert checkpoint_pull_requests([intent, receipt], [checkpoint], "2" * 32, "forge/one") == [
        ("https://github.com/team/product/pull/7", "b" * 64)
    ]


@pytest.mark.parametrize(
    "change",
    [
        "later_unknown",
        "orphan",
        "different_revision",
        "different_checkpoint",
        "duplicate_receipt",
        "different_repository",
    ],
)
def test_unconfirmed_or_substituted_reference_is_refused(change):
    checkpoint, intent, receipt = evidence()
    entries = [intent, receipt]
    if change == "later_unknown":
        entries.append({**copy.deepcopy(intent), "idempotency_key": "ready-one", "sequence": 3, "digest": "e" * 64})
    elif change == "orphan":
        entries = [receipt]
    elif change == "different_revision":
        intent["payload"]["revision"] = receipt["payload"]["revision"] = "f" * 40
    elif change == "different_checkpoint":
        intent["payload"]["checkpoint_digest"] = "f" * 64
    elif change == "duplicate_receipt":
        entries.append(copy.deepcopy(receipt))
    else:
        intent["payload"]["repository_id"] = "3" * 32
    with pytest.raises(ValueError):
        checkpoint_pull_requests(entries, [checkpoint], "2" * 32, "forge/one")
