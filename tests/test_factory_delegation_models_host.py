# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Versioned child work is explicit; old captures do not acquire new authority."""

import pytest
from pydantic import ValidationError

from firefly_forge.delegation_models import (
    DelegationCancelInput,
    DelegationJoinInput,
    DelegationStartInput,
    DelegationWaitInput,
    FactoryTeamV1,
    FamilyBudget,
)


def test_new_team_policy_uses_the_approved_envelope_and_no_work_deadline():
    team = FactoryTeamV1(budget=FamilyBudget(requests=100, tool_calls=300, commands=22))
    assert team.protocol == "forge-factory-team-v1"
    assert team.automatic is True
    assert "timeout_seconds" not in team.model_dump()
    assert team.budget.commands == 22


def test_family_approval_objects_cannot_be_mutated_into_larger_grants():
    team = FactoryTeamV1(budget=FamilyBudget(requests=100, tool_calls=300, commands=22))
    with pytest.raises(ValidationError, match="frozen"):
        team.budget.requests = 101
    with pytest.raises(ValidationError, match="frozen"):
        team.automatic = False


def test_start_is_idempotently_named_analysis_or_owned_source_proposal():
    analysis = DelegationStartInput(request_id="a" * 32, task_id="inspect-api", brief="Inspect the approved API")
    assert analysis.mode == "analysis" and analysis.write_paths == []
    proposal = DelegationStartInput(
        request_id="b" * 32,
        task_id="api-fix",
        brief="Prepare the approved API fix",
        mode="proposal",
        write_paths=["src/api.py", "tests"],
    )
    assert proposal.write_paths == ["src/api.py", "tests"]


@pytest.mark.parametrize("path", ["../outside", "/absolute", ".git/config", ".env", "x/../y", "x\\y", "x//y"])
def test_proposed_source_ownership_cannot_escape_public_paths(path):
    with pytest.raises(ValidationError):
        DelegationStartInput(
            request_id="a" * 32, task_id="fix", brief="Approved fix", mode="proposal", write_paths=[path]
        )


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "proposal"},
        {"mode": "analysis", "write_paths": ["source.py"]},
        {"mode": "proposal", "write_paths": ["src", "src"]},
        {"request_id": "not-a-nonce"},
        {"provider": "unapproved-model"},
        {"publish": True},
        {"version": True},
    ],
)
def test_child_input_cannot_create_implicit_source_or_provider_grants(body):
    with pytest.raises(ValidationError):
        DelegationStartInput.model_validate({"request_id": "a" * 32, "task_id": "fix", "brief": "Approved fix", **body})


@pytest.mark.parametrize("value", [True, 0, -1, "3"])
def test_family_budget_is_exact_positive_requests_and_nonnegative_effects(value):
    with pytest.raises(ValidationError):
        FamilyBudget(requests=value, tool_calls=10, commands=1)
    assert FamilyBudget(requests=1, tool_calls=0, commands=0).commands == 0


def test_wait_is_one_typed_observation_with_a_durable_cursor_and_optional_timeout():
    value = DelegationWaitInput(delegation_ids=["a" * 32, "b" * 32], after_sequence=7)
    assert value.timeout_seconds is None
    assert value.after_sequence == 7
    assert DelegationWaitInput(delegation_ids=["a" * 32], timeout_seconds=864000).timeout_seconds == 864000
    with pytest.raises(ValidationError):
        DelegationWaitInput(delegation_ids=["a" * 32, "a" * 32])


def test_join_and_cancel_preserve_exact_parent_chosen_child_identity():
    assert DelegationJoinInput(request_id="c" * 32, delegation_ids=["b" * 32, "a" * 32]).delegation_ids == [
        "b" * 32,
        "a" * 32,
    ]
    assert DelegationCancelInput(request_id="d" * 32, delegation_id="a" * 32).delegation_id == "a" * 32
