# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""New-run nullable command deadlines do not relax finite legacy setup or recovery bodies."""

import pytest
from pydantic import ValidationError

from firefly_forge.models import Command, NewWorkCommand, RunInput, configured_commands


@pytest.mark.parametrize("timeout", [None, 864000])
def test_new_run_accepts_explicit_nullable_or_chosen_long_deadline(timeout):
    body = RunInput(repository_id="fixture", brief="Inspect", commands=[{"argv": ["fixture"], "timeout": timeout}])
    assert body.commands[0].timeout == timeout
    assert body.model_dump()["commands"] == [{"argv": ["fixture"], "timeout": timeout}]


def test_new_command_default_is_unlimited_but_legacy_command_default_and_wire_stay_exact():
    assert NewWorkCommand(argv=["fixture"]).timeout is None
    old = Command(argv=["fixture"])
    assert old.model_dump() == {"argv": ["fixture"], "timeout": 300}
    with pytest.raises(ValidationError):
        Command(argv=["fixture"], timeout=None)
    legacy_run = {"commands": [old.model_dump()]}
    assert configured_commands(legacy_run)[0].model_dump() == old.model_dump()
    with pytest.raises(ValidationError):
        configured_commands({"commands": [{"argv": ["fixture"], "timeout": None}]})


def test_nullable_runtime_commands_require_explicit_frozen_new_work_policy():
    run = {
        "work_duration": {"protocol": "forge-work-duration-v2", "timeout_seconds": None},
        "commands": [{"argv": ["fixture"], "timeout": None}],
    }
    assert configured_commands(run)[0].timeout is None
    with pytest.raises(ValidationError):
        configured_commands({**run, "work_duration": {"protocol": "forged"}})


def test_legacy_omitted_command_timeout_keeps_normalized_request_wire():
    body = RunInput(repository_id="fixture", brief="Inspect", commands=[{"argv": ["fixture"]}])
    assert body.model_dump()["commands"] == [{"argv": ["fixture"], "timeout": 300}]


def test_new_capture_preserves_explicit_caps_and_removes_only_implicit_legacy_default():
    from firefly_forge.models import capture_new_work_commands

    values = [
        Command(argv=["default"]),
        Command(argv=["chosen"], timeout=300),
        NewWorkCommand(argv=["unlimited"], timeout=None),
    ]
    before = [value.model_dump() for value in values]
    assert capture_new_work_commands(values) == [
        {"argv": ["default"], "timeout": None},
        {"argv": ["chosen"], "timeout": 300},
        {"argv": ["unlimited"], "timeout": None},
    ]
    assert [value.model_dump() for value in values] == before
