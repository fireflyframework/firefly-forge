# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Automatic framework checks inherit the selected run policy, not an invented cap."""

import pytest

from firefly_forge.framework_knowledge_conformance import conformance_commands
from firefly_forge.work_duration_models import WorkDurationPolicyV2


@pytest.mark.parametrize("framework", ["pyfly", "larafly"])
@pytest.mark.parametrize(
    "policy,expected",
    [(None, 600), (WorkDurationPolicyV2(), None), (WorkDurationPolicyV2(timeout_seconds=864000), None)],
)
def test_actual_framework_check_arguments_preserve_legacy_or_selected_work_policy(framework, policy, expected):
    from firefly_forge.engine import framework_commands

    pin = {"framework": framework, "framework_version": "26.10.11"}
    run = {"framework_knowledge_pin": pin}
    if policy is not None:
        run["work_duration"] = policy.model_dump(mode="json")
    commands = framework_commands(run)
    assert [command.argv for command in commands] == conformance_commands(pin)
    assert all(command.timeout == expected for command in commands)
    assert run["framework_knowledge_pin"] == pin


def test_no_framework_pin_adds_no_automatic_command_and_unknown_duration_is_refused():
    from firefly_forge.engine import framework_commands

    assert framework_commands({}) == []
    with pytest.raises(ValueError):
        framework_commands(
            {
                "framework_knowledge_pin": {"framework": "pyfly", "framework_version": "26.10.11"},
                "work_duration": {"protocol": "unknown"},
            }
        )
