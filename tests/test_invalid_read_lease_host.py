# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A fresh ownership lease never creates a fresh attempt or usage allowance."""

import importlib
import importlib.util

import pytest


def api():
    name = "firefly_forge.invalid_read_lease"
    assert importlib.util.find_spec(name) is not None, "Explicit same-attempt lease policy is missing"
    return importlib.import_module(name)


def test_expired_original_deadline_does_not_replace_remaining_active_budget():
    module = api()
    policy = module.ContinuityLeasePolicyV1(version=1, enabled=True, max_window_seconds=60)
    lease = module.lease_window(
        policy,
        frozen_timeout_seconds=30,
        valid_for_seconds=20,
        now_ms=100000,
        original_deadline_ms=1,
        active_deadline_ms=115000,
    )
    assert lease == {
        "original_deadline_ms": 1,
        "issued_at_ms": 100000,
        "expires_at_ms": 115000,
        "window_ms": 15000,
        "active_deadline_ms": 115000,
    }


@pytest.mark.parametrize("case", ["disabled", "timeout", "policy", "active_budget", "zero"])
def test_lease_does_not_bypass_disabled_policy_or_time_bounds(case):
    module = api()
    policy = module.ContinuityLeasePolicyV1(version=1, enabled=case != "disabled", max_window_seconds=60)
    with pytest.raises(ValueError):
        module.lease_window(
            policy,
            frozen_timeout_seconds=30,
            valid_for_seconds={"timeout": 31, "policy": 61, "zero": 0}.get(case, 20),
            now_ms=100000,
            original_deadline_ms=1,
            active_deadline_ms=100000 if case == "active_budget" else None,
        )


def test_missing_policy_is_disabled_and_configuration_revision_is_explicit(tmp_path):
    from firefly_forge.configuration import ConfigurationStore

    module = api()
    private = ConfigurationStore(tmp_path / "private")
    absent = module.policy_snapshot(private)
    assert absent["settings"]["enabled"] is False and absent["revision"] is None
    configured = module.write_policy(
        private, module.ContinuityLeasePolicyV1(version=1, enabled=True, max_window_seconds=60)
    )
    assert configured["revision"] and configured["digest"] != absent["digest"]
    assert module.policy_snapshot(private) == configured
    revoked = module.write_policy(
        private, module.ContinuityLeasePolicyV1(version=1, enabled=False, max_window_seconds=60)
    )
    assert revoked["revision"] != configured["revision"] and not revoked["settings"]["enabled"]


def test_queue_limits_are_elastic_positive_admin_settings(tmp_path):
    from firefly_forge.configuration import ConfigurationStore

    module = api()
    private = ConfigurationStore(tmp_path / "elastic-policy")
    policy = module.ContinuityLeasePolicyV1(
        version=1, enabled=True, max_tickets_per_run=5000, max_pending_tickets=50000
    )
    configured = module.write_policy(private, policy)
    assert configured["settings"]["max_tickets_per_run"] == 5000
    assert configured["settings"]["max_pending_tickets"] == 50000
    assert module.policy_snapshot(private) == configured
    schema = module.ContinuityLeasePolicyV1.model_json_schema()["properties"]
    assert "maximum" not in schema["max_tickets_per_run"]
    assert "maximum" not in schema["max_pending_tickets"]


@pytest.mark.parametrize("field", ["max_tickets_per_run", "max_pending_tickets"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "501", float("inf")])
def test_queue_limits_still_require_positive_finite_integer_counts(field, value):
    with pytest.raises(ValueError):
        api().ContinuityLeasePolicyV1.model_validate({"version": 1, field: value})
