# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Evaluate actual scanner output without model approval or runtime network lookups."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from firefly_forge.journal import digest
from firefly_forge.process import ProcessResult
from firefly_forge.quality_gate_models import (
    QualityBindingV1,
    QualityFindingV1,
    QualityGateContractV1,
    QualityLocationV1,
    QualityObservationV1,
    quality_observation,
    quality_path,
)


def _json(value: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        if len(dict(pairs)) != len(pairs):
            raise ValueError("Duplicate report fields")
        return dict(pairs)

    def invalid(value: str) -> None:
        raise ValueError("Non-finite report value")

    return json.loads(value, object_pairs_hook=unique, parse_constant=invalid)


def _report_path(uri: str, source_root: Path | None) -> str:
    reference = urlsplit(uri)
    if reference.query or reference.fragment or reference.netloc:
        raise ValueError("External report paths are unsupported")
    if reference.scheme == "file" and source_root is not None:
        path = Path(unquote(reference.path))
        if ".." in path.parts:
            raise ValueError("Report paths cannot traverse the project root")
        return quality_path(path.relative_to(source_root).as_posix())
    if reference.scheme:
        raise ValueError("Report paths must resolve inside the captured project root")
    return quality_path(unquote(reference.path))


def _sarif(
    output: str, contract: QualityGateContractV1, version: str, source_root: Path | None
) -> list[QualityFindingV1]:
    report = _json(output)
    if (
        not isinstance(report, dict)
        or report.get("version") != "2.1.0"
        or not isinstance(report.get("runs"), list)
        or not report["runs"]
        or report.get("inlineExternalProperties")
    ):
        raise ValueError("Unsupported or incomplete SARIF log")
    findings = []
    for run in report["runs"]:
        if not isinstance(run, dict) or run.get("externalPropertyFileReferences"):
            raise ValueError("External report properties are unsupported")
        driver = run["tool"]["driver"]
        if driver["name"] != contract.tool.name or any(
            driver[key] != version for key in ("version", "semanticVersion") if key in driver
        ):
            raise ValueError("SARIF belongs to another tool or version")
        invocations = run.get("invocations", [])
        if not isinstance(invocations, list):
            raise ValueError("Invalid SARIF invocation metadata")
        for invocation in invocations:
            if (
                invocation.get("executionSuccessful") is not True
                or invocation.get("ruleConfigurationOverrides")
                or any(
                    item.get("level", "warning") in {"warning", "error"}
                    for field in ("toolExecutionNotifications", "toolConfigurationNotifications")
                    for item in invocation.get(field, [])
                )
            ):
                raise ValueError("The tool did not establish successful complete analysis")
        if not isinstance(run.get("results"), list):
            raise ValueError("SARIF acceptance requires an explicit complete results array")
        rules = driver.get("rules", [])
        for result in run["results"]:
            if result.get("kind", "fail") != "fail" or result.get("baselineState") == "absent":
                raise ValueError("This SARIF result kind cannot establish current findings")
            rule: dict[str, Any] | None = None
            if "ruleIndex" in result:
                index = result["ruleIndex"]
                if type(index) is not int or not 0 <= index < len(rules):
                    raise ValueError("SARIF rule index is unavailable")
                rule = rules[index]
            identifier = result.get("ruleId") or (rule or {}).get("id")
            if not identifier or rule and result.get("ruleId", identifier) != rule["id"]:
                raise ValueError("SARIF rule identity is unavailable or contradictory")
            if rule is None:
                rule = next((item for item in rules if item.get("id") == identifier), {})
            level = result.get("level", rule.get("defaultConfiguration", {}).get("level", "warning"))
            message = result["message"]["text"]
            locations = result.get("locations", [])
            if not locations:
                locations = [{}]
            references = []
            for location in locations:
                physical = location.get("physicalLocation", {})
                artifact = physical.get("artifactLocation", {})
                if artifact.get("uriBaseId") or "index" in artifact:
                    raise ValueError("Indirect SARIF paths require an explicit supported resolver")
                path = _report_path(artifact["uri"], source_root) if "uri" in artifact else None
                references.append(QualityLocationV1(path=path, line=physical.get("region", {}).get("startLine")))
            findings.append(
                QualityFindingV1(
                    rule_id=identifier,
                    level=level,
                    message=message,
                    path=references[0].path,
                    line=references[0].line,
                    locations=references,
                    suppressed=bool(result.get("suppressions")),
                )
            )
    return findings


def observe_quality(
    gate_id: str,
    contract: QualityGateContractV1,
    binding: QualityBindingV1,
    result: ProcessResult,
    *,
    expected_version: str,
    phase: Literal["prerequisite", "check"] = "check",
    prerequisite: QualityObservationV1 | None = None,
    output_truncated: bool = False,
    source_root: Path | None = None,
    attested_tool_identity: str | None = None,
    logical_argv: list[str] | None = None,
) -> QualityObservationV1:
    status, reason, version = "indeterminate", "prerequisite_unavailable", None
    identity = None
    findings: list[QualityFindingV1] = []
    contract_digest = digest(contract.model_dump(mode="json"))
    try:
        if not expected_version or type(result.exit_code) is not int:
            raise ValueError("An exact locked tool version and process exit are required")
        if contract.tool.approved_version != expected_version:
            raise ValueError("The lock differs from the project-approved tool version; configure its prerequisite")
        if binding.configuration_digest != digest(contract.approved_configuration):
            raise ValueError("Current rule configuration differs from the approved policy")
        if output_truncated:
            raise ValueError("The complete report is unavailable; increase the configured report budget")
        if phase == "prerequisite":
            if (logical_argv if logical_argv is not None else result.argv) != contract.tool.probe.argv:
                raise ValueError("The observed prerequisite command changed")
            if contract.tool.version_prefix is not None:
                text = result.output.strip()
                prefix = contract.tool.version_prefix
                if "\n" in text or not text.startswith(prefix) or text[len(prefix) :].split()[:1] != [expected_version]:
                    raise ValueError("Tool version output differs from the locked prerequisite")
                observed = {
                    "package": contract.tool.package,
                    "version": expected_version,
                    "identity_digest": attested_tool_identity,
                }
            else:
                observed = _json(result.output)
            if (
                not isinstance(observed, dict)
                or set(observed) != {"package", "version", "identity_digest"}
                or observed["package"] != contract.tool.package
                or observed["version"] != expected_version
                or not isinstance(observed["identity_digest"], str)
                or re.fullmatch(r"[a-f0-9]{64}", observed["identity_digest"]) is None
            ):
                raise ValueError("The installed tool differs from the selected project lock")
            version = expected_version
            identity = observed["identity_digest"]
            status, reason = ("passed", "locked_tool_observed") if result.exit_code == 0 else ("failed", "tool_failed")
        else:
            if prerequisite is not None:
                prerequisite = QualityObservationV1.model_validate(prerequisite.model_dump(mode="json"))
            if (
                prerequisite is None
                or prerequisite.status != "passed"
                or prerequisite.phase != "prerequisite"
                or prerequisite.gate_id != gate_id
                or prerequisite.binding != binding
                or prerequisite.contract_digest != contract_digest
                or prerequisite.tool_version != expected_version
            ):
                raise ValueError("A matching current tool prerequisite is required")
            version = expected_version
            identity = prerequisite.tool_identity_digest
            if contract.format == "sarif-2.1.0":
                findings = _sarif(result.output, contract, expected_version, source_root)
            elif contract.format != "exit-status":
                raise ValueError("Unsupported result adapter")
            rejected = sum(item.level in contract.fail_levels for item in findings) > contract.max_findings
            status = "failed" if result.exit_code != 0 or rejected else "passed"
            reason = "findings_exceed_policy" if rejected else "tool_failed" if result.exit_code else "checks_passed"
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        reason = "incomplete_or_unsupported_report" if phase == "check" else "locked_tool_not_established"
        status = "failed" if result.exit_code != 0 and not output_truncated else "indeterminate"
    return quality_observation(
        gate_id=gate_id,
        phase=phase,
        contract=contract.model_dump(mode="json"),
        contract_digest=contract_digest,
        binding=binding.model_dump(mode="json"),
        command_digest=digest({"argv": result.argv}),
        logical_command_digest=digest({"argv": logical_argv if logical_argv is not None else result.argv}),
        exit_code=result.exit_code,
        status=status,
        tool_version=version,
        tool_identity_digest=identity,
        report_digest=hashlib.sha256(result.output.encode()).hexdigest(),
        report_bytes=len(result.output.encode()),
        output_truncated=output_truncated,
        findings=[item.model_dump(mode="json") for item in findings],
        reason=reason,
        prerequisite_digest=prerequisite.digest if prerequisite is not None else None,
    )
