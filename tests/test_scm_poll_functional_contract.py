# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Functional polling is a separate finite allowance, never an upgraded V2 binding."""

import importlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from firefly_forge.scm_poll_models import SCMPollActorBindingInputV2


def grant():
    return dict(
        version=1,
        transport="github_rest_poll_v1",
        request_id="a" * 32,
        expected_previous_digest=None,
        expected_project_binding_digest="b" * 64,
        expected_binding_request_id="c" * 32,
        expected_binding_digest="d" * 64,
        component_ids=["api"],
        issue_number=4,
        allowed_actions=["assistant_start", "assistant_status", "assistant_turn"],
        max_model_turns=3,
        expires_at=(datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        model_profile_id="functional",
        expected_model_profile_revision=2,
        expected_model_profile_digest="e" * 64,
        expected_routing_digest="f" * 64,
        expected_functional_policy_digest="9" * 64,
        active=True,
        acknowledge_separate_assistant_allowance=True,
    )


def test_functional_grant_is_separate_from_immutable_status_answer_consent():
    model = importlib.import_module("firefly_forge.scm_poll_functional_models").SCMPollFunctionalGrantInputV1
    body = grant()
    assert model.model_validate(body).model_dump(mode="json") == body
    with pytest.raises(ValidationError):
        SCMPollActorBindingInputV2.model_validate(body)


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"transport": "signed_webhook_v1"},
        {"max_model_turns": 4},
        {"max_model_turns": True},
        {"max_model_turns": 0},
        {"allowed_actions": ["resume"]},
        {"allowed_actions": ["assistant_start", "assistant_start"]},
        {"component_ids": ["api", "api"]},
        {"acknowledge_separate_assistant_allowance": 1},
        {"active": 1},
        {"expires_at": "2026-10-22T10:00:00"},
        {"run_budget": 35},
    ],
)
def test_functional_consent_refuses_coercion_and_authority_expansion(change):
    model = importlib.import_module("firefly_forge.scm_poll_functional_models").SCMPollFunctionalGrantInputV1
    with pytest.raises(ValidationError):
        model.model_validate({**grant(), **change})


def test_expiration_and_reserved_turn_are_finite_and_replays_do_not_refund():
    m = importlib.import_module("firefly_forge.scm_poll_functional")
    body = grant()
    now = datetime.now(UTC)
    m.validate_expiration(body, now)
    with pytest.raises(ValueError, match="60 minutes"):
        m.validate_expiration({**body, "expires_at": (now + timedelta(minutes=61)).isoformat()}, now)
    with pytest.raises(ValueError, match="expired"):
        m.validate_expiration({**body, "expires_at": now.isoformat()}, now)
    state = m.functional_state({})
    state["turns"]["a" * 64] = []
    for ordinal in range(3):
        assert m.reserve_turn(state, "a" * 64, str(ordinal), 3) is True
        assert m.reserve_turn(state, "a" * 64, str(ordinal), 3) is False
    with pytest.raises(ValueError, match="exhausted"):
        m.reserve_turn(state, "a" * 64, "fourth", 3)
