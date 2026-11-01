# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Declared check scope, rather than a fixed list size, controls check selection."""

import pytest

from firefly_forge.application_operations import MaintenanceRequest
from firefly_forge.coding_check_models import CodingCheckGrantInput, ConfiguredCheckRequest, configured_command
from firefly_forge.models import RunInput
from firefly_forge.project_models import ProjectComponent


def commands():
    return [{"argv": ["python", "-m", "pytest", f"tests/component_{index}"]} for index in range(137)]


@pytest.mark.parametrize("kind", ["run", "component", "maintenance"])
def test_complete_declared_verification_suite_survives_admission_and_serialization(kind):
    checks = commands()
    if kind == "run":
        model = RunInput(repository_id="a" * 32, brief="Verify all product components", commands=checks)
        field = "commands"
    elif kind == "maintenance":
        model = MaintenanceRequest(brief="Verify the maintained product", commands=checks)
        field = "commands"
    else:
        model = ProjectComponent(
            id="service", name="Service", repository_id="a" * 32, responsibility="Own the API", verification=checks
        )
        field = "verification"
    restored = type(model).model_validate_json(model.model_dump_json())
    actual = restored.model_dump()[field]
    assert len(actual) == 137
    assert [check["argv"] for check in actual] == [check["argv"] for check in checks]
    selected = configured_command(actual, [136], ConfiguredCheckRequest(command_index=136, request_id="b" * 32))
    assert selected.argv == ["python", "-m", "pytest", "tests/component_136"]


def test_large_explicit_check_grant_keeps_its_declared_indices_and_call_allowance():
    grant = CodingCheckGrantInput(
        request_id="a" * 32,
        recovery_request_id="b" * 32,
        candidate_digest="c" * 64,
        expected_revision=0,
        command_indices=list(range(137)),
        call_limit=137,
        valid_seconds=300,
        reason="Review the complete configured component suite",
    )
    restored = CodingCheckGrantInput.model_validate_json(grant.model_dump_json())
    assert restored.command_indices == list(range(137)) and restored.call_limit == 137


@pytest.mark.parametrize("index,allowed", [(137, [137]), (136, [0])])
def test_large_suite_still_refuses_missing_or_ungranted_check_indices(index, allowed):
    request = ConfiguredCheckRequest(command_index=index, request_id="b" * 32)
    with pytest.raises(ValueError, match="outside the granted configured scope"):
        configured_command(commands(), allowed, request)


@pytest.mark.parametrize("index", [-1, True, "136"])
def test_check_indices_remain_strict_nonnegative_integers(index):
    with pytest.raises(ValueError):
        ConfiguredCheckRequest(command_index=index, request_id="b" * 32)
