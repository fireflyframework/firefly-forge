# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Immutable public pool policy and typed commands, without runtime credentials."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from firefly_forge.runner_bundle_models import ProjectBundleIdentity
from firefly_forge.sandbox_models import bounded_argv, pinned_image


def pool_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class FactoryFence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    factory_schema: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    pid: int = Field(gt=0, strict=True)
    backend_started_at: datetime

    @field_validator("backend_started_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Database backend identity requires a timezone")
        return value


class RunnerInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    execution_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    profile_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    project_id: UUID | None = None
    repository_id: UUID
    run_id: UUID
    image: str = Field(max_length=512)
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    cwd: str = Field(default=".", max_length=512)
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    archive_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_bytes: int = Field(ge=0, le=268_435_456, strict=True)
    stdin_bytes: int = Field(default=0, ge=0, le=2_097_152, strict=True)
    stdin_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    cpus: float = Field(default=1, ge=0.1, le=4, allow_inf_nan=False)
    memory_mb: int = Field(default=512, ge=128, le=8192, strict=True)
    pids_limit: int = Field(default=128, ge=32, le=512, strict=True)
    timeout: int = Field(default=300, ge=1, le=3600, strict=True)
    max_output_bytes: int = Field(default=1_000_000, ge=1024, le=2_000_000, strict=True)

    _image = field_validator("image")(pinned_image)

    @field_validator("argv")
    @classmethod
    def bounded_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        bounded_argv(list(value))
        return value

    @field_validator("cwd")
    @classmethod
    def relative_directory(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value or "\x00" in value:
            raise ValueError("Runner cwd must stay within its workspace")
        return str(path)

    @model_validator(mode="after")
    def stdin_identity(self) -> RunnerInvocation:
        if (self.stdin_bytes == 0) != (self.stdin_digest is None):
            raise ValueError("Nonempty command stdin requires its immutable digest")
        return self

    @property
    def project_key(self) -> str:
        return ("project:" + self.project_id.hex) if self.project_id else ("repository:" + self.repository_id.hex)


class RunnerProjectInvocation(RunnerInvocation):
    protocol: Literal["forge-project-invocation-v1"] = "forge-project-invocation-v1"
    bundle: ProjectBundleIdentity

    @model_validator(mode="after")
    def framed_identity(self) -> RunnerProjectInvocation:
        if not self.bundle.git_archive_bytes:
            raise ValueError("Project invocation requires the minimum frozen Git snapshot")
        if (self.archive_digest, self.source_bytes, self.source_digest) != (
            self.bundle.archive_digest,
            self.bundle.archive_bytes,
            self.bundle.source_digest,
        ):
            raise ValueError("Project invocation differs from its frozen bundle identity")
        return self


def parse_invocation(value: Any) -> RunnerInvocation:
    if isinstance(value, dict) and "protocol" in value:
        return RunnerProjectInvocation.model_validate(value)
    return RunnerInvocation.model_validate(value)


class RunnerPoolProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol: Literal["forge-local-pool-v1"] = "forge-local-pool-v1"
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    daemon_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")
    approved_images: tuple[str, ...] = Field(min_length=1, max_length=32)
    transfer_image: str = Field(max_length=512)
    network: Literal["none"] = "none"
    slots: int = Field(default=2, ge=1, le=64, strict=True)
    project_slots: int = Field(default=1, ge=1, le=64, strict=True)
    cpu_budget: float = Field(default=2, ge=0.1, le=256, allow_inf_nan=False)
    memory_budget_mb: int = Field(default=2048, ge=128, le=524288, strict=True)
    project_cpu_budget: float = Field(default=4, ge=0.1, le=256, allow_inf_nan=False)
    project_memory_budget_mb: int = Field(default=8192, ge=128, le=524288, strict=True)
    max_cpus: float = Field(default=2, ge=0.1, le=4, allow_inf_nan=False)
    max_memory_mb: int = Field(default=1024, ge=128, le=8192, strict=True)
    max_pids: int = Field(default=128, ge=32, le=512, strict=True)
    max_timeout: int = Field(default=300, ge=1, le=3600, strict=True)
    max_source_bytes: int = Field(default=67_108_864, ge=1_048_576, le=268_435_456, strict=True)
    max_output_bytes: int = Field(default=1_000_000, ge=1024, le=2_000_000, strict=True)
    waiting_limit: int = Field(default=100, ge=1, le=1000, strict=True)
    admission_seconds: int = Field(default=120, ge=1, le=3600, strict=True)
    lease_seconds: int = Field(default=30, ge=1, le=120, strict=True)

    _transfer = field_validator("transfer_image")(pinned_image)

    @field_validator("approved_images")
    @classmethod
    def pinned_images(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for image in value:
            pinned_image(image)
        if len(set(value)) != len(value):
            raise ValueError("Duplicate approved image")
        return value

    @model_validator(mode="after")
    def capacity(self) -> RunnerPoolProfile:
        if self.project_slots > self.slots:
            raise ValueError("Project quota exceeds total slots")
        return self

    @property
    def revision(self) -> str:
        return pool_digest(self.model_dump(mode="json"))

    def validate_invocation(self, value: RunnerInvocation) -> None:
        if value.profile_id != self.profile_id or value.profile_revision != self.revision:
            raise ValueError("Invocation does not select this immutable profile")
        self.validate_budget(
            image=value.image,
            cpus=value.cpus,
            memory_mb=value.memory_mb,
            pids_limit=value.pids_limit,
            timeout=value.timeout,
            source_bytes=value.source_bytes,
            max_output_bytes=value.max_output_bytes,
        )

    def validate_budget(
        self,
        *,
        image: str,
        cpus: float,
        memory_mb: int,
        pids_limit: int,
        timeout: int,
        source_bytes: int,
        max_output_bytes: int,
    ) -> None:
        if image not in self.approved_images:
            raise ValueError("Invocation image is not approved")
        bounds = (
            (cpus, min(self.max_cpus, self.cpu_budget, self.project_cpu_budget)),
            (memory_mb, min(self.max_memory_mb, self.memory_budget_mb, self.project_memory_budget_mb)),
            (pids_limit, self.max_pids),
            (timeout, self.max_timeout),
            (source_bytes, self.max_source_bytes),
            (max_output_bytes, self.max_output_bytes),
        )
        if any(requested > granted for requested, granted in bounds):
            raise ValueError("Invocation exceeds administrator profile budget")
