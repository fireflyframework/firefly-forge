# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit source allowance parameters never reset original lifetime limits."""

import importlib

import pytest


def module():
    return importlib.import_module("firefly_forge.source_budget_allowances")


def proposal():
    return {"version": 1, "read_file_lifetime_limit": 20, "activation_ceiling": 5, "additional_activations": 1}


def test_parameters_are_bounded_explicit_and_policy_is_default_denied():
    m = module()
    body = m.SourceBudgetProposal.model_validate(proposal())
    policy = m.SourceBudgetPolicy()
    with pytest.raises(ValueError, match="disabled"):
        m.checked_parameters(body, policy, previous_attempt=4, used_allowances=0)
    allowed = m.SourceBudgetPolicy(
        enabled=True, max_read_file_lifetime_limit=20, max_activation_ceiling=5, max_additional_activations=1
    )
    assert m.checked_parameters(body, allowed, previous_attempt=4, used_allowances=0) == {
        "read_file_lifetime_limit": 20,
        "activation_ceiling": 5,
        "additional_activations": 1,
    }


@pytest.mark.parametrize("change", ["bool", "reset", "jump", "unbounded", "extra", "version"])
def test_invalid_request_cannot_become_a_magic_fifth_attempt(change):
    m = module()
    body = proposal()
    if change == "bool":
        body["additional_activations"] = True
    if change == "reset":
        body["read_file_lifetime_limit"] = 1
    if change == "jump":
        body["activation_ceiling"] = 6
    if change == "unbounded":
        body["additional_activations"] = 1000
    if change == "extra":
        body["commands_limit"] = 100
    if change == "version":
        body["version"] = True
    with pytest.raises(ValueError):
        b = m.SourceBudgetProposal.model_validate(body)
        m.checked_parameters(
            b,
            m.SourceBudgetPolicy(
                enabled=True, max_read_file_lifetime_limit=20, max_activation_ceiling=5, max_additional_activations=1
            ),
            previous_attempt=4,
            used_allowances=0,
        )


def test_policy_and_consumed_allowances_both_fence_the_next_activation():
    m = module()
    b = m.SourceBudgetProposal.model_validate(proposal())
    p = m.SourceBudgetPolicy(
        enabled=True, max_read_file_lifetime_limit=20, max_activation_ceiling=5, max_additional_activations=1
    )
    with pytest.raises(ValueError):
        m.checked_parameters(b, p, previous_attempt=4, used_allowances=1)
    with pytest.raises(ValueError):
        m.checked_parameters(
            b, p.model_copy(update={"max_read_file_lifetime_limit": 19}), previous_attempt=4, used_allowances=0
        )
    with pytest.raises(ValueError):
        m.checked_parameters(b, p, previous_attempt=3, used_allowances=0)


def test_legacy_human_link_still_rejects_attempt_four():
    from firefly_forge.human_clarifications import _Link

    with pytest.raises(ValueError):
        _Link.model_validate(
            {
                "version": 1,
                "request_id": "question",
                "feedback_digest": "a" * 64,
                "response_digest": "b" * 64,
                "from_attempt": 4,
                "to_attempt": 5,
                "resuming_actor": {"user_id": "a" * 32, "authentication_method": "local_bootstrap"},
            }
        )


def test_versioned_source_budget_feedback_does_not_widen_ordinary_feedback():
    from firefly_forge.agent_roles import WorkerFeedback, worker_feedback_model

    body = {
        "protocol": "source-budget-continuation-v1",
        "id": "controlled-feedback",
        "role_id": "implementer",
        "task_id": "implement",
        "attempt": 5,
        "disposition": "completed",
        "summary": "A bounded source edit is ready for native checks",
        "allowance_id": "a" * 32,
        "allowance_digest": "b" * 64,
    }
    result = worker_feedback_model(body).model_validate(body)
    assert result.attempt == 5 and result.protocol == "source-budget-continuation-v1"
    with pytest.raises(ValueError):
        WorkerFeedback.model_validate(
            {k: v for k, v in body.items() if k not in {"protocol", "allowance_id", "allowance_digest"}}
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_digest", "not-a-digest"),
        ("status", "unknown"),
        ("correlation_id", ""),
        ("tool", "unbounded tool name with spaces"),
    ],
)
def test_safe_history_requires_bounded_typed_immutable_headers(field, value):
    m = module()
    row = {
        "correlation_id": "a" * 32,
        "request_digest": "b" * 64,
        "status": "succeeded",
        "admission": {"tool": "read_file"},
    }
    if field == "tool":
        row["admission"][field] = value
    else:
        row[field] = value
    with pytest.raises(ValueError):
        m.history_snapshot([row])
