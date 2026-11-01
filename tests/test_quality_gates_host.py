# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Deterministic quality observations never derive authority from an AI review."""

import copy
import json

import pytest

from firefly_forge.journal import digest
from firefly_forge.process import ProcessResult
from firefly_forge.quality_gate_models import QualityBindingV1, QualityGateContractV1
from firefly_forge.quality_gates import observe_quality


def contract(format="sarif-2.1.0"):
    return QualityGateContractV1.model_validate(
        {
            "version": 1,
            "tool": {
                "package": "example-analyzer",
                "name": "Example analyzer",
                "ecosystem": "python",
                "lock_file": "uv.lock",
                "approved_version": "1.2.3",
                "probe": {"argv": ["locked-python", "tool-version"], "timeout": 30},
            },
            "format": format,
            "configuration_paths": ["pyproject.toml"],
            "approved_configuration": {"pyproject.toml": "7" * 64},
        }
    )


def binding():
    return QualityBindingV1(
        run_id="1" * 32,
        source_revision="2" * 40,
        source_tree="3" * 40,
        policy_digest="4" * 64,
        runner_digest="5" * 64,
        lock_digest="6" * 64,
        configuration_digest=digest({"pyproject.toml": "7" * 64}),
        framework_digest="8" * 64,
    )


def tool_observation(spec, version="1.2.3"):
    return {"package": spec.tool.package, "version": version, "identity_digest": "9" * 64}


def sarif(results=None):
    return {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "Example analyzer", "version": "1.2.3"}},
                "invocations": [{"executionSuccessful": True}],
                "results": [] if results is None else results,
            }
        ],
    }


def observe(report=None, *, format="sarif-2.1.0", exit_code=0, truncated=False, prerequisite=None):
    spec, scope = contract(format), binding()
    if prerequisite is None:
        prerequisite = observe_quality(
            "quality:0",
            spec,
            scope,
            ProcessResult(spec.tool.probe.argv, 0, json.dumps(tool_observation(spec))),
            expected_version="1.2.3",
            phase="prerequisite",
            attested_tool_identity="9" * 64,
        )
    output = json.dumps(sarif() if report is None else report) if format != "exit-status" else "Checks passed"
    return observe_quality(
        "quality:0",
        spec,
        scope,
        ProcessResult(["locked-analyzer", "check"], exit_code, output),
        expected_version="1.2.3",
        prerequisite=prerequisite,
        output_truncated=truncated,
    )


def test_success_requires_an_actual_successful_prerequisite_and_complete_report():
    result = observe()
    assert result.status == "passed" and result.findings == []
    assert result.prerequisite_digest and result.report_bytes > 0
    assert result.binding == binding()


@pytest.mark.parametrize("change", ["failed_invocation", "missing_results", "external", "version"])
def test_sarif_incomplete_or_unexecuted_reports_never_pass(change):
    report = sarif()
    run = report["runs"][0]
    if change == "failed_invocation":
        run["invocations"][0]["executionSuccessful"] = False
    elif change == "missing_results":
        del run["results"]
    elif change == "external":
        run["externalPropertyFileReferences"] = {"results": [{"location": {"uri": "https://invalid.invalid/data"}}]}
    else:
        run["tool"]["driver"]["version"] = "9.0.0"
    assert observe(report).status == "indeterminate"


def test_sarif_optional_invocations_use_the_recorded_process_not_an_invented_invocation():
    report = sarif()
    del report["runs"][0]["invocations"]
    assert observe(report).status == "passed"
    report["runs"][0]["invocations"] = [{}]
    assert observe(report).status == "indeterminate"


def test_real_tool_absolute_file_uri_must_be_inside_the_captured_project_root(tmp_path):
    spec, scope = contract(), binding()
    prerequisite = observe_quality(
        "quality:0",
        spec,
        scope,
        ProcessResult(spec.tool.probe.argv, 0, json.dumps(tool_observation(spec))),
        expected_version="1.2.3",
        phase="prerequisite",
        attested_tool_identity="9" * 64,
    )
    report = sarif(
        [
            {
                "ruleId": "Q1",
                "message": {"text": "Actual finding"},
                "locations": [{"physicalLocation": {"artifactLocation": {"uri": (tmp_path / "main.py").as_uri()}}}],
            }
        ]
    )
    result = observe_quality(
        "quality:0",
        spec,
        scope,
        ProcessResult(["analyzer"], 0, json.dumps(report)),
        expected_version="1.2.3",
        prerequisite=prerequisite,
        source_root=tmp_path,
    )
    assert result.status == "failed" and result.findings[0].path == "main.py"
    result = observe_quality(
        "quality:0",
        spec,
        scope,
        ProcessResult(["analyzer"], 0, json.dumps(report)),
        expected_version="1.2.3",
        prerequisite=prerequisite,
        source_root=tmp_path / "other",
    )
    assert result.status == "indeterminate"


def test_sarif_default_warning_and_suppression_do_not_waive_findings():
    finding = {
        "ruleId": "Q1",
        "message": {"text": "Unsafe operation"},
        "suppressions": [{"kind": "inSource", "status": "accepted"}],
        "baselineState": "unchanged",
        "locations": [{"physicalLocation": {"artifactLocation": {"uri": "src/main.py"}, "region": {"startLine": 7}}}],
    }
    result = observe(sarif([finding]))
    assert result.status == "failed"
    assert result.exit_code == 0
    assert result.findings[0].level == "warning"
    assert result.findings[0].rule_id == "Q1" and result.findings[0].path == "src/main.py"
    assert result.findings[0].line == 7 and result.findings[0].suppressed is True


def test_sarif_rule_index_and_default_configuration_are_resolved():
    report = sarif([{"ruleIndex": 0, "message": {"text": "Information"}}])
    report["runs"][0]["tool"]["driver"]["rules"] = [{"id": "Q2", "defaultConfiguration": {"level": "note"}}]
    result = observe(report)
    assert result.findings[0].level == "note" and result.findings[0].rule_id == "Q2"
    assert result.status == "passed"


@pytest.mark.parametrize("change", ["rule", "path", "missing_message", "absent_baseline", "kind"])
def test_unsupported_findings_are_indeterminate_instead_of_disappearing(change):
    finding = {"ruleId": "Q1", "message": {"text": "Actual finding"}}
    if change == "rule":
        finding.pop("ruleId")
    elif change == "path":
        finding["locations"] = [{"physicalLocation": {"artifactLocation": {"uri": "../../private"}}}]
    elif change == "missing_message":
        finding.pop("message")
    elif change == "absent_baseline":
        finding["baselineState"] = "absent"
    else:
        finding["kind"] = "pass"
    assert observe(sarif([finding])).status == "indeterminate"


@pytest.mark.parametrize("format", ["exit-status", "sarif-2.1.0"])
def test_truncated_and_nonzero_outputs_preserve_physical_exit_and_do_not_pass(format):
    assert observe(format=format, truncated=True).status == "indeterminate"
    result = observe(format=format, exit_code=3)
    assert result.status == "failed" and result.exit_code == 3


@pytest.mark.parametrize(
    "change", ["lock_digest", "source_tree", "policy_digest", "runner_digest", "configuration_digest"]
)
def test_an_old_prerequisite_cannot_approve_a_new_cut(change):
    spec, scope = contract(), binding()
    old = observe_quality(
        "quality:0",
        spec,
        scope,
        ProcessResult(spec.tool.probe.argv, 0, json.dumps(tool_observation(spec))),
        expected_version="1.2.3",
        phase="prerequisite",
        attested_tool_identity="9" * 64,
    )
    value = scope.model_dump()
    value[change] = "a" * len(value[change])
    result = observe_quality(
        "quality:0",
        spec,
        QualityBindingV1.model_validate(value),
        ProcessResult(["locked-analyzer", "check"], 0, json.dumps(sarif())),
        expected_version="1.2.3",
        prerequisite=old,
    )
    assert result.status == "indeterminate"


def test_missing_or_wrong_tool_is_not_an_accepted_prerequisite():
    spec = contract()
    for code, output in [(127, "tool missing"), (0, '{"package":"example-analyzer","version":"0.1"}')]:
        result = observe_quality(
            "quality:0",
            spec,
            binding(),
            ProcessResult(spec.tool.probe.argv, code, output),
            expected_version="1.2.3",
            phase="prerequisite",
            attested_tool_identity="9" * 64,
        )
        assert result.status != "passed"


def test_duplicate_json_keys_cannot_replace_a_failing_report():
    spec = contract()
    result = observe_quality(
        "quality:0",
        spec,
        binding(),
        ProcessResult(spec.tool.probe.argv, 0, '{"package":"example-analyzer","version":"0.1","version":"1.2.3"}'),
        expected_version="1.2.3",
        phase="prerequisite",
        attested_tool_identity="9" * 64,
    )
    assert result.status == "indeterminate"


def test_observation_digest_and_declared_contract_are_immutable():
    value = observe().model_dump(mode="json")
    assert value["contract_digest"] == digest(contract().model_dump(mode="json"))
    before = copy.deepcopy(value)
    value["binding"]["source_tree"] = "f" * 40
    from firefly_forge.quality_gate_models import QualityObservationV1

    with pytest.raises(ValueError):
        QualityObservationV1.model_validate(value)
    assert before["binding"]["source_tree"] == "3" * 40


def test_multiple_locations_are_one_finding_and_all_locations_are_retained():
    spec = contract().model_copy(update={"max_findings": 1})
    prerequisite = observe_quality(
        "quality:0",
        spec,
        binding(),
        ProcessResult(spec.tool.probe.argv, 0, json.dumps(tool_observation(spec))),
        expected_version="1.2.3",
        phase="prerequisite",
        attested_tool_identity="9" * 64,
    )
    report = sarif(
        [
            {
                "ruleId": "Q1",
                "message": {"text": "One issue across two files"},
                "locations": [
                    {"physicalLocation": {"artifactLocation": {"uri": path}, "region": {"startLine": line}}}
                    for path, line in [("first.py", 3), ("second.py", 5)]
                ],
            }
        ]
    )
    result = observe_quality(
        "quality:0",
        spec,
        binding(),
        ProcessResult(["analyzer"], 0, json.dumps(report)),
        expected_version="1.2.3",
        prerequisite=prerequisite,
    )
    assert result.status == "passed" and len(result.findings) == 1
    assert [(p.path, p.line) for p in result.findings[0].locations] == [("first.py", 3), ("second.py", 5)]


def test_mutated_prerequisite_is_not_authority_even_with_matching_visible_fields():
    spec = contract()
    prerequisite = observe_quality(
        "quality:0",
        spec,
        binding(),
        ProcessResult(spec.tool.probe.argv, 0, json.dumps(tool_observation(spec))),
        expected_version="1.2.3",
        phase="prerequisite",
        attested_tool_identity="9" * 64,
    )
    object.__setattr__(prerequisite, "report_digest", "e" * 64)
    assert observe(prerequisite=prerequisite).status == "indeterminate"


def test_recapturing_an_agent_weakened_configuration_cannot_approve_it():
    spec = contract()
    changed = binding().model_copy(update={"configuration_digest": digest({"pyproject.toml": "a" * 64})})
    result = observe_quality(
        "quality:0",
        spec,
        changed,
        ProcessResult(spec.tool.probe.argv, 0, json.dumps(tool_observation(spec))),
        expected_version="1.2.3",
        phase="prerequisite",
        attested_tool_identity="9" * 64,
    )
    assert result.status == "indeterminate"


def test_sha256_git_revisions_are_supported_without_a_new_repository_limit():
    actual = binding().model_copy(update={"source_revision": "a" * 64})
    assert QualityBindingV1.model_validate(actual.model_dump()).source_revision == "a" * 64


def test_a_version_string_without_tool_payload_identity_cannot_pass():
    spec = contract()
    result = observe_quality(
        "quality:0",
        spec,
        binding(),
        ProcessResult(spec.tool.probe.argv, 0, json.dumps({"package": spec.tool.package, "version": "1.2.3"})),
        expected_version="1.2.3",
        phase="prerequisite",
    )
    assert result.status == "indeterminate"


def test_closed_native_version_probe_uses_existing_dependency_attestation():
    value = contract().model_dump(mode="json")
    value["tool"].update(version_prefix="ruff ", probe={"argv": ["python", "-B", "-m", "ruff", "--version"]})
    spec = QualityGateContractV1.model_validate(value)
    for identity in (None, "a" * 64):
        result = observe_quality(
            "quality:0",
            spec,
            binding(),
            ProcessResult(spec.tool.probe.argv, 0, "ruff 1.2.3\n"),
            expected_version="1.2.3",
            phase="prerequisite",
            attested_tool_identity=identity,
        )
        assert result.status == ("passed" if identity else "indeterminate")
        assert result.tool_identity_digest == identity


def test_closed_wrapper_preserves_physical_and_logical_command_bindings():
    value = contract().model_dump(mode="json")
    value["tool"].update(version_prefix="ruff ", probe={"argv": ["python", "-B", "-m", "ruff", "--version"]})
    spec = QualityGateContractV1.model_validate(value)
    physical = ["/owned/sandbox-exec", "-p", "captured-profile", *spec.tool.probe.argv]
    result = observe_quality(
        "quality:0",
        spec,
        binding(),
        ProcessResult(physical, 0, "ruff 1.2.3\n"),
        expected_version="1.2.3",
        phase="prerequisite",
        attested_tool_identity="a" * 64,
        logical_argv=spec.tool.probe.argv,
    )
    assert result.status == "passed"
    assert result.command_digest == digest({"argv": physical})
    assert result.logical_command_digest == digest({"argv": spec.tool.probe.argv})


@pytest.mark.parametrize("approved", [None, "9.0.0"])
def test_a_new_lock_cannot_approve_a_different_tool_version_by_itself(approved):
    value = contract().model_dump(mode="json")
    value["tool"]["approved_version"] = approved
    spec = QualityGateContractV1.model_validate(value)
    result = observe_quality(
        "quality:0",
        spec,
        binding(),
        ProcessResult(spec.tool.probe.argv, 0, json.dumps(tool_observation(spec))),
        expected_version="1.2.3",
        phase="prerequisite",
    )
    assert result.status == "indeterminate"


def test_boolean_contract_version_is_not_a_wire_version():
    value = contract().model_dump(mode="json")
    value["version"] = True
    with pytest.raises(ValueError):
        QualityGateContractV1.model_validate(value)


def test_boolean_observation_version_cannot_be_normalized_before_digest_validation():
    from firefly_forge.quality_gate_models import QualityObservationV1

    value = observe().model_dump(mode="json")
    value["version"] = True
    with pytest.raises(ValueError):
        QualityObservationV1.model_validate(value)


@pytest.mark.parametrize(
    "category", ["maintainability", "coverage", "dependencies", "secrets", "licenses", "architecture", "accessibility"]
)
def test_quality_categories_do_not_imply_an_installed_scanner(category):
    value = contract().model_dump(mode="json")
    value["category"] = category
    assert QualityGateContractV1.model_validate(value).category == category


def test_a_json_scanner_cannot_print_its_own_payload_authority():
    spec = contract()
    result = observe_quality(
        "quality:0",
        spec,
        binding(),
        ProcessResult(spec.tool.probe.argv, 0, json.dumps(tool_observation(spec))),
        expected_version="1.2.3",
        phase="prerequisite",
        attested_tool_identity="b" * 64,
    )
    assert result.status == "indeterminate"
