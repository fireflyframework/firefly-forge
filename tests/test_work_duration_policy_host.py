# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""New work has no implicit duration cap; explicit caps stay strict and immutable."""

import pytest
from pydantic import ValidationError

from firefly_forge.work_duration_models import WorkDurationPolicyV2, effective_work_timeout


def test_new_work_duration_defaults_to_no_deadline():
    policy = WorkDurationPolicyV2()
    assert policy.model_dump(mode="json") == {"protocol": "forge-work-duration-v2", "timeout_seconds": None}
    assert effective_work_timeout(policy.timeout_seconds, None) is None


@pytest.mark.parametrize("values,expected", [((), None), ((None, None), None), ((None, 4), 4), ((8, None, 3), 3)])
def test_only_explicit_positive_caps_limit_work(values, expected):
    assert effective_work_timeout(*values) == expected


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "300"])
def test_invalid_duration_cannot_become_an_implicit_or_unlimited_policy(value):
    with pytest.raises(ValidationError):
        WorkDurationPolicyV2(timeout_seconds=value)
    with pytest.raises(ValueError):
        effective_work_timeout(value)


def test_explicit_cap_is_immutable_and_does_not_inherit_legacy_hour_ceiling():
    policy = WorkDurationPolicyV2(timeout_seconds=7201)
    assert effective_work_timeout(policy.timeout_seconds) == 7201
    with pytest.raises(ValidationError, match="frozen"):
        policy.timeout_seconds = 4
    with pytest.raises(ValidationError):
        WorkDurationPolicyV2(protocol="forge-work-duration-v1")
