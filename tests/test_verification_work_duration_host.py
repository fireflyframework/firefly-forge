# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Versioned verification intents bind nullable caps without rewriting legacy custody."""

from types import SimpleNamespace

import pytest

from firefly_forge import verification_handoffs as handoff
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
from firefly_forge.work_duration_models import WorkDurationPolicyV2


@pytest.mark.parametrize(
    "policy,requested,expected",
    [
        (None, 2, 2),
        (WorkDurationPolicyV2(), None, None),
        (WorkDurationPolicyV2(timeout_seconds=12), None, 12),
        (WorkDurationPolicyV2(timeout_seconds=12), 8, 8),
    ],
)
def test_intent_version_and_timeout_are_selected_from_exact_frozen_policy(
    tmp_path, monkeypatch, policy, requested, expected
):
    frozen = {"digest": "a" * 64}
    run = {
        "commands": [{"argv": ["fixture"], "timeout": requested}],
        "tool_policy_snapshot": frozen,
        "sandbox_snapshot": capture_sandbox(SandboxInput(**({"work_duration": policy} if policy else {}))),
        "worker_request": {"attempt": 1, "id": "fixture"},
    }
    if policy is not None:
        run["work_duration"] = policy.model_dump(mode="json")
    monkeypatch.setattr(handoff, "automatic_candidate", lambda _: True)
    monkeypatch.setattr(handoff, "automatic_verification_grant", lambda _: SimpleNamespace(max_continuations=4))
    monkeypatch.setattr(handoff, "tool_policy_snapshot", lambda _: frozen)
    monkeypatch.setattr(handoff, "verification_source", lambda _: "b" * 64)
    monkeypatch.setattr(handoff, "verification_provider", lambda *args: "c" * 64)
    monkeypatch.setattr(handoff, "verification_scope", lambda _: "d" * 64)
    intent = handoff.verification_intent(SimpleNamespace(configuration=tmp_path), run, tmp_path, [], [])
    assert intent["version"] == (1 if policy is None else 2)
    assert intent["timeouts"] == [expected]
    assert intent["ordinals"] == [1] and intent["commands_count"] == 1
    if policy is not None:
        run["work_duration"] = WorkDurationPolicyV2(timeout_seconds=1).model_dump(mode="json")
        with pytest.raises(ValueError, match="duration"):
            handoff.verification_intent(SimpleNamespace(configuration=tmp_path), run, tmp_path, [], [])
