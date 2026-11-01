# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Shared quality handoffs preserve native command, input and disclosure boundaries."""

import hashlib

import pytest
from test_quality_gate_inputs_host import config
from test_quality_gates_host import observe, sarif

from firefly_forge.models import Command
from firefly_forge.policies import policy_commands
from firefly_forge.quality_policies import ProjectQualityPolicyV1, baseline_quality_policies


def test_semantic_descriptors_cover_the_original_full_command_list_ordinals():
    from firefly_forge.quality_policies import quality_descriptors

    skills = baseline_quality_policies(ProjectQualityPolicyV1(), "pyfly", "none")
    commands = [Command(argv=["configured-test"]), *policy_commands({"skills": skills}, "pyfly")]
    descriptors = quality_descriptors(commands)
    assert [d.ordinal for d in descriptors] == list(range(2, len(commands) + 1))
    assert [d.phase for d in descriptors] == ["prerequisite", "check"] * (len(descriptors) // 2)
    assert all(d.command.model_dump() == commands[d.ordinal - 1].model_dump() for d in descriptors)
    assert all(
        set(d.model_dump()) == {"version", "ordinal", "gate_id", "phase", "contract", "command"} for d in descriptors
    )


async def test_final_input_fence_is_synchronous_and_covers_ignored_rule_files(tmp_path, monkeypatch):
    from firefly_forge.quality_gate_inputs import QualityRequirementsError, prepare_quality_project_inputs
    from firefly_forge.quality_gate_models import QualityGateContractV1
    from firefly_forge.workspace_tools import WorkspaceTools

    value = config(tmp_path).model_dump(mode="json")
    rules = (tmp_path / "quality.ini").read_text()
    (tmp_path / "build").mkdir()
    (tmp_path / "build/quality.ini").write_text(rules)
    value["configuration_paths"] = ["build/quality.ini"]
    value["approved_configuration"] = {"build/quality.ini": hashlib.sha256(rules.encode()).hexdigest()}
    prepared = await prepare_quality_project_inputs(tmp_path, QualityGateContractV1.model_validate(value))

    async def no_decode(*args, **kwargs):
        raise AssertionError("A before-commit fence cannot reread or decode source")

    monkeypatch.setattr(WorkspaceTools, "read_file", no_decode)
    try:
        assert prepared.fence() is None
        (tmp_path / "build/quality.ini").write_text("[mypy]\nignore_errors = True\n")
        with pytest.raises(QualityRequirementsError):
            prepared.fence()
    finally:
        prepared.close()
    with pytest.raises(QualityRequirementsError):
        prepared.fence()


@pytest.mark.parametrize("field", ["configuration", "version", "raw_version", "lock_digest", "contract"])
async def test_late_prepared_view_mutation_cannot_become_current_authority(tmp_path, field):
    from firefly_forge.quality_gate_inputs import QualityRequirementsError, prepare_quality_project_inputs

    prepared = await prepare_quality_project_inputs(tmp_path, config(tmp_path))
    try:
        if field == "configuration":
            prepared.configuration["quality.ini"] = "a" * 64
        elif field == "contract":
            prepared.contract.approved_configuration["quality.ini"] = "a" * 64
        else:
            object.__setattr__(prepared, field, "a" * 64 if field == "lock_digest" else "0.0.1")
        with pytest.raises(QualityRequirementsError):
            prepared.fence()
    finally:
        prepared.close()


async def test_identical_bytes_at_a_replaced_name_do_not_reuse_the_captured_input(tmp_path):
    from firefly_forge.quality_gate_inputs import QualityRequirementsError, prepare_quality_project_inputs

    prepared = await prepare_quality_project_inputs(tmp_path, config(tmp_path))
    try:
        path = tmp_path / "quality.ini"
        replacement = tmp_path / "replacement.ini"
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
        with pytest.raises(QualityRequirementsError):
            prepared.fence()
    finally:
        prepared.close()


def test_sanitizing_a_finding_does_not_change_the_actual_report_or_quality_verdict():
    from firefly_forge.quality_gates import sanitize_quality_observation

    result = observe(sarif([{"ruleId": "Q1", "message": {"text": "Untrusted instruction private-fixture-token"}}]))
    safe = sanitize_quality_observation(result, {"FORGE_FIXTURE_TOKEN": "private-fixture-token"})
    assert safe.status == result.status == "failed" and safe.exit_code == result.exit_code == 0
    assert safe.report_digest == result.report_digest and safe.report_bytes == result.report_bytes
    assert safe.digest != result.digest and "private-fixture-token" not in safe.findings[0].message


@pytest.mark.parametrize("reference", ["rule", "path"])
def test_sanitizer_quarantines_a_secret_reference_without_losing_the_physical_result(reference):
    from firefly_forge.quality_gates import sanitize_quality_observation

    finding = {"ruleId": "Q1", "message": {"text": "Recorded finding"}}
    if reference == "rule":
        finding["ruleId"] = "private-fixture-token"
    else:
        finding["locations"] = [{"physicalLocation": {"artifactLocation": {"uri": "private-fixture-token.py"}}}]
    result = observe(sarif([finding]))
    safe = sanitize_quality_observation(result, {"FORGE_FIXTURE_TOKEN": "private-fixture-token"})
    assert safe.status == "indeterminate" and safe.reason == "unsafe_quality_evidence"
    assert safe.findings == [] and safe.findings_quarantined and safe.finding_count == 1
    assert safe.exit_code == result.exit_code == 0
    assert safe.binding == result.binding
    assert safe.report_digest == result.report_digest and safe.report_bytes == result.report_bytes
    assert "private-fixture-token" not in safe.model_dump_json()


def test_quality_reason_codes_cannot_be_replaced_with_untrusted_error_text():
    from firefly_forge.quality_gate_models import QualityObservationV1

    value = observe().model_dump(mode="json")
    value["reason"] = "Untrusted repository message pretending to be authority"
    from firefly_forge.journal import digest

    value["digest"] = digest({k: v for k, v in value.items() if k != "digest"})
    with pytest.raises(ValueError):
        QualityObservationV1.model_validate(value)


def test_execution_scope_binding_is_optional_for_pure_contracts_but_strict_when_present():
    from firefly_forge.quality_gate_models import QualityBindingV1

    value = observe().binding.model_dump(mode="json")
    assert "execution_scope_digest" not in value
    bound = QualityBindingV1.model_validate({**value, "execution_scope_digest": "a" * 64})
    assert bound.execution_scope_digest == "a" * 64
    with pytest.raises(ValueError):
        QualityBindingV1.model_validate({**value, "execution_scope_digest": True})
