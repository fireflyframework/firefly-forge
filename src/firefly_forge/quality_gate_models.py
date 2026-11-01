# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Frozen quality contracts and observations; native execution supplies their authority."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from firefly_forge.journal import digest
from firefly_forge.models import Command

Level = Literal["none", "note", "warning", "error"]


def quality_version(value: Any) -> Any:
    if type(value) is not int or value != 1:
        raise ValueError("Quality protocol requires the exact integer version 1")
    return value


def quality_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != value
        or "\\" in value
        or ":" in value
        or any(ord(c) < 32 for c in value)
        or any(part in {".git", ".env", ".ssh", ".forge"} or part.startswith(".env.") for part in path.parts)
    ):
        raise ValueError("Quality references require a relative non-private project file")
    return value


class QualityToolV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    package: str = Field(min_length=1)
    name: str = Field(min_length=1)
    ecosystem: Literal["python", "composer", "node"]
    lock_file: str
    probe: Command
    version_prefix: str | None = Field(default=None, min_length=1)
    approved_version: str | None = Field(default=None, min_length=1)

    _path = field_validator("lock_file")(quality_path)


class QualityGateContractV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    tool: QualityToolV1
    category: Literal["lint", "format", "types", "tests", "build", "security"] = "lint"
    format: Literal["exit-status", "sarif-2.1.0"] = "exit-status"
    configuration_paths: list[str] = Field(default_factory=list)
    approved_configuration: dict[str, str] = Field(default_factory=dict)
    fail_levels: list[Level] = Field(default=["error", "warning"])
    max_findings: int = Field(default=0, ge=0)

    _version = field_validator("version", mode="before")(quality_version)

    @field_validator("configuration_paths")
    @classmethod
    def relative_configuration(cls, values: list[str]) -> list[str]:
        return [quality_path(value) for value in values]

    @model_validator(mode="after")
    def unique_inputs(self) -> QualityGateContractV1:
        if len(set(self.configuration_paths)) != len(self.configuration_paths) or len(set(self.fail_levels)) != len(
            self.fail_levels
        ):
            raise ValueError("Quality input paths and threshold levels must be unique")
        if set(self.configuration_paths) != set(self.approved_configuration):
            raise ValueError("Every rule configuration requires its policy-approved fingerprint")
        import re

        if any(re.fullmatch(r"[a-f0-9]{64}", value) is None for value in self.approved_configuration.values()):
            raise ValueError("Approved rule configurations require SHA256 fingerprints")
        return self


class QualityBindingV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    source_revision: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    source_tree: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    runner_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    lock_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    configuration_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    framework_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class QualityLocationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    path: str | None = None
    line: int | None = Field(default=None, ge=1)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str | None) -> str | None:
        return quality_path(value) if value is not None else None

    @model_validator(mode="after")
    def source_line(self) -> QualityLocationV1:
        if self.line is not None and self.path is None:
            raise ValueError("A quality finding line needs its relative file")
        return self


class QualityFindingV1(QualityLocationV1):
    rule_id: str = Field(min_length=1)
    level: Level
    message: str = Field(min_length=1)
    suppressed: bool = False
    locations: list[QualityLocationV1] = Field(default_factory=list)


class QualityObservationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    gate_id: str = Field(pattern=r"^[a-z][a-z0-9-]*:[0-9]+$")
    phase: Literal["prerequisite", "check"]
    contract: QualityGateContractV1
    contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    binding: QualityBindingV1
    command_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    logical_command_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    exit_code: int
    status: Literal["passed", "failed", "indeterminate"]
    tool_version: str | None
    tool_identity_digest: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    report_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    report_bytes: int = Field(ge=0)
    output_truncated: bool
    findings: list[QualityFindingV1]
    reason: str = Field(min_length=1)
    prerequisite_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    _version = field_validator("version", mode="before")(quality_version)

    @model_validator(mode="after")
    def sealed_observation(self) -> QualityObservationV1:
        if self.contract_digest != digest(self.contract.model_dump(mode="json")) or self.digest != digest(
            self.model_dump(mode="json", exclude={"digest"})
        ):
            raise ValueError("Quality observation or declared contract changed")
        if self.status == "passed" and (
            self.exit_code != 0
            or self.output_truncated
            or not self.tool_version
            or self.tool_version != self.contract.tool.approved_version
            or self.tool_identity_digest is None
            or self.binding.configuration_digest != digest(self.contract.approved_configuration)
            or self.phase == "check"
            and not self.prerequisite_digest
            or sum(item.level in self.contract.fail_levels for item in self.findings) > self.contract.max_findings
        ):
            raise ValueError("Passing quality evidence contradicts its actual observation")
        return self


def quality_observation(**values: Any) -> QualityObservationV1:
    value = {"version": 1, "prerequisite_digest": None, **values}
    return QualityObservationV1.model_validate({**value, "digest": digest(value)})
