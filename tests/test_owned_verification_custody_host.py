# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Verification timeout semantics opt in without weakening legacy command proofs."""

import pytest

from firefly_forge.checkpoint_mode_handoff_custody import verification_commands
from firefly_forge.sandbox_models import SandboxInput
from firefly_forge.work_duration_models import WorkDurationPolicyV2


def run(policy=None, timeout=None):
    value = {"commands": [{"argv": ["fictional-check"], "timeout": timeout}]}
    if policy is not None:
        selected = policy.model_dump(mode="json")
        value.update(work_duration=selected, sandbox_snapshot={"configuration": {"work_duration": selected}})
    return value


@pytest.mark.parametrize(
    "cap,command,expected", [(None, None, None), (None, 8000, 8000), (9000, None, 9000), (9000, 12, 12)]
)
def test_new_verification_preserves_explicit_caps_without_using_legacy_default(cap, command, expected):
    commands, timeouts, version = verification_commands(
        run(WorkDurationPolicyV2(timeout_seconds=cap), command), SandboxInput(timeout=1)
    )
    assert commands[0].timeout == command and timeouts == [expected] and version == 2


def test_legacy_verification_keeps_its_integer_timeout_and_version():
    commands, timeouts, version = verification_commands(run(timeout=600), SandboxInput(timeout=300))
    assert commands[0].timeout == 600 and timeouts == [300] and version == 1
    with pytest.raises(ValueError):
        verification_commands(run(), SandboxInput())


def test_nullable_commands_cannot_enter_under_mismatched_frozen_selection():
    value = run(WorkDurationPolicyV2())
    value["sandbox_snapshot"]["configuration"] = {}
    with pytest.raises(ValueError):
        verification_commands(value, SandboxInput())
