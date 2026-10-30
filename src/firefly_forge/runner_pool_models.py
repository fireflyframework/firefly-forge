# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Immutable public pool policy and typed commands, without runtime credentials."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from firefly_forge.runner_bundle_models import ProjectBundleIdentity
from firefly_forge.runner_kubernetes_models import KubernetesPoolPolicy, cpu_quantity, memory_quantity
from firefly_forge.runner_run_liveness import RUN_LIVENESS_PROTOCOL, RunLivenessBindingV1
from firefly_forge.sandbox_models import bounded_argv, pinned_image
from firefly_forge.work_duration_models import WorkDurationPolicyV2


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
    timeout: int | None = Field(default=300, ge=1, strict=True)
    work_duration: WorkDurationPolicyV2 | None = None
    run_liveness: RunLivenessBindingV1 | None = None
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
        if self.run_liveness is not None and self.work_duration is None:
            raise ValueError("Run ownership requires the explicit new work-duration policy")
        if self.work_duration is None:
            if self.timeout is None or self.timeout > 3600:
                raise ValueError("A legacy invocation requires its finite duration")
        elif self.timeout != self.work_duration.timeout_seconds:
            raise ValueError("Invocation timeout differs from its explicit work duration")
        if (self.stdin_bytes == 0) != (self.stdin_digest is None):
            raise ValueError("Nonempty command stdin requires its immutable digest")
        return self

    @model_serializer(mode="wrap")
    def retained_duration(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        value: dict[str, Any] = handler(self)
        if self.work_duration is None:
            value.pop("work_duration")
        if self.run_liveness is None:
            value.pop("run_liveness")
        return value

    @property
    def project_key(self) -> str:
        return ("project:" + self.project_id.hex) if self.project_id else ("repository:" + self.repository_id.hex)


class RunnerProjectInvocation(RunnerInvocation):
    protocol: Literal["forge-project-invocation-v1"] = "forge-project-invocation-v1"
    bundle: ProjectBundleIdentity

    @model_validator(mode="after")
    def framed_identity(self) -> RunnerProjectInvocation:
        validate_project_identity(self, self.bundle)
        return self


def validate_project_identity(invocation: RunnerInvocation, bundle: ProjectBundleIdentity) -> None:
    if not bundle.git_archive_bytes:
        raise ValueError("Project invocation requires the minimum frozen Git snapshot")
    if (invocation.archive_digest, invocation.source_bytes, invocation.source_digest) != (
        bundle.archive_digest,
        bundle.archive_bytes,
        bundle.source_digest,
    ):
        raise ValueError("Project invocation differs from its frozen bundle identity")


class KubernetesProjectInvocation(RunnerInvocation):
    protocol: Literal["forge-kubernetes-project-invocation-v1"] = "forge-kubernetes-project-invocation-v1"
    bundle: ProjectBundleIdentity
    cpus: float = Field(gt=0, allow_inf_nan=False)
    memory_mb: int = Field(ge=1, strict=True)
    pids_limit: int = Field(ge=1, strict=True)
    work_duration: WorkDurationPolicyV2

    @model_validator(mode="after")
    def framed_identity(self) -> KubernetesProjectInvocation:
        cpu_quantity(self.cpus)
        memory_quantity(self.memory_mb)
        validate_project_identity(self, self.bundle)
        return self


def parse_invocation(value: Any) -> RunnerInvocation:
    if isinstance(value, dict) and value.get("protocol") == "forge-kubernetes-project-invocation-v1":
        return KubernetesProjectInvocation.model_validate(value)
    if isinstance(value, dict) and "protocol" in value:
        return RunnerProjectInvocation.model_validate(value)
    return RunnerInvocation.model_validate(value)


class RunnerPoolProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol: Literal["forge-local-pool-v1", "forge-kubernetes-pool-v1"] = "forge-local-pool-v1"
    backend: Literal["docker", "kubernetes"] = "docker"
    kubernetes: KubernetesPoolPolicy | None = None
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    daemon_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")
    approved_images: tuple[str, ...] = Field(min_length=1, max_length=32)
    transfer_image: str = Field(max_length=512)
    network: Literal["none"] = "none"
    slots: int = Field(default=2, ge=1, strict=True)
    project_slots: int = Field(default=1, ge=1, strict=True)
    cpu_budget: float = Field(default=2, gt=0, allow_inf_nan=False)
    memory_budget_mb: int = Field(default=2048, ge=1, strict=True)
    project_cpu_budget: float = Field(default=4, gt=0, allow_inf_nan=False)
    project_memory_budget_mb: int = Field(default=8192, ge=1, strict=True)
    max_cpus: float = Field(default=2, gt=0, allow_inf_nan=False)
    max_memory_mb: int = Field(default=1024, ge=1, strict=True)
    max_pids: int = Field(default=128, ge=1, strict=True)
    max_timeout: int = Field(default=300, ge=1, le=3600, strict=True)
    work_duration: WorkDurationPolicyV2 | None = None
    run_liveness: Literal["forge-run-owned-liveness-v1"] | None = None
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
        if self.backend == "kubernetes":
            if self.protocol != "forge-kubernetes-pool-v1" or self.kubernetes is None:
                raise ValueError("Kubernetes execution requires its explicit protocol and isolation policy")
            required = {
                "slots",
                "project_slots",
                "cpu_budget",
                "memory_budget_mb",
                "project_cpu_budget",
                "project_memory_budget_mb",
                "max_cpus",
                "max_memory_mb",
                "max_pids",
                "max_timeout",
            }
            if not required <= self.model_fields_set:
                raise ValueError("Kubernetes capacity and duration must be selected explicitly")
        else:
            if self.protocol != "forge-local-pool-v1" or self.kubernetes is not None:
                raise ValueError("A Docker pool cannot inherit Kubernetes authority")
            legacy_bounds = (
                (self.slots, 1, 64),
                (self.project_slots, 1, 64),
                (self.cpu_budget, 0.1, 256),
                (self.project_cpu_budget, 0.1, 256),
                (self.memory_budget_mb, 128, 524288),
                (self.project_memory_budget_mb, 128, 524288),
                (self.max_cpus, 0.1, 4),
                (self.max_memory_mb, 128, 8192),
                (self.max_pids, 32, 512),
            )
            if any(not minimum <= value <= maximum for value, minimum, maximum in legacy_bounds):
                raise ValueError("Docker capacity exceeds its legacy protocol bounds")
        if self.run_liveness is not None and self.work_duration is None:
            raise ValueError("Run ownership requires an explicit administrator work-duration policy")
        if self.project_slots > self.slots:
            raise ValueError("Project quota exceeds total slots")
        return self

    @model_serializer(mode="wrap")
    def retained_duration(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        value: dict[str, Any] = handler(self)
        if self.backend == "docker":
            value.pop("backend")
            value.pop("kubernetes")
        if self.work_duration is None:
            value.pop("work_duration")
        if self.run_liveness is None:
            value.pop("run_liveness")
        return value

    @property
    def revision(self) -> str:
        return pool_digest(self.model_dump(mode="json"))

    def validate_invocation(self, value: RunnerInvocation) -> None:
        if (self.backend == "kubernetes") != isinstance(value, KubernetesProjectInvocation):
            raise ValueError(
                "Invocation must select the exact Kubernetes invocation protocol or its legacy Docker backend"
            )
        if (self.run_liveness == RUN_LIVENESS_PROTOCOL) != (value.run_liveness is not None):
            raise ValueError("Invocation run ownership differs from the immutable administrator profile")
        if value.profile_id != self.profile_id or value.profile_revision != self.revision:
            raise ValueError("Invocation does not select this immutable profile")
        self.validate_budget(
            image=value.image,
            cpus=value.cpus,
            memory_mb=value.memory_mb,
            pids_limit=value.pids_limit,
            timeout=value.timeout,
            work_duration=value.work_duration,
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
        timeout: int | None,
        work_duration: WorkDurationPolicyV2 | None = None,
        source_bytes: int,
        max_output_bytes: int,
    ) -> None:
        if image not in self.approved_images:
            raise ValueError("Invocation image is not approved")
        if work_duration is None:
            if timeout is None or timeout > self.max_timeout:
                raise ValueError("Invocation exceeds administrator duration budget")
        elif (
            self.work_duration is None
            or timeout != work_duration.timeout_seconds
            or (
                self.work_duration.timeout_seconds is not None
                and (timeout is None or timeout > self.work_duration.timeout_seconds)
            )
        ):
            raise ValueError("Invocation exceeds administrator work-duration policy")
        bounds = (
            (cpus, min(self.max_cpus, self.cpu_budget, self.project_cpu_budget)),
            (memory_mb, min(self.max_memory_mb, self.memory_budget_mb, self.project_memory_budget_mb)),
            (pids_limit, self.max_pids),
            (source_bytes, self.max_source_bytes),
            (max_output_bytes, self.max_output_bytes),
        )
        if any(requested > granted for requested, granted in bounds):
            raise ValueError("Invocation exceeds administrator profile budget")
