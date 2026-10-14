# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit new factory pool policies; existing frozen runner captures are unchanged."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pyfly.core.config import Config

from firefly_forge.runner_controller_config import RunnerWorkerTransport, absolute_path, private_tls_file
from firefly_forge.runner_pool_models import RunnerPoolProfile, pool_digest
from firefly_forge.sandbox_models import pinned_image


class FactoryPoolSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol: Literal["forge-factory-pool-v1"] = "forge-factory-pool-v1"
    mode: Literal["pool"] = "pool"
    profile: RunnerPoolProfile
    image: str = Field(max_length=512)
    workspace_transport: Literal["forge-project-bundle-v1"] = "forge-project-bundle-v1"
    workspace_max_bytes: int = Field(default=67_108_864, ge=1_048_576, le=268_435_456, strict=True)
    cpus: float = Field(default=1, ge=0.1, le=4, allow_inf_nan=False)
    memory_mb: int = Field(default=512, ge=128, le=8192, strict=True)
    pids_limit: int = Field(default=128, ge=32, le=512, strict=True)
    timeout: int = Field(default=300, ge=1, le=3600, strict=True)
    max_output_bytes: int = Field(default=1_000_000, ge=1024, le=2_000_000, strict=True)
    command_limit: int = Field(default=50, ge=1, le=100, strict=True)
    network: Literal["none"] = "none"
    internet_search: bool = False

    _image = field_validator("image")(pinned_image)

    @model_validator(mode="after")
    def bounded_policy(self) -> FactoryPoolSelection:
        self.profile.validate_budget(
            image=self.image,
            cpus=self.cpus,
            memory_mb=self.memory_mb,
            pids_limit=self.pids_limit,
            timeout=self.timeout,
            source_bytes=self.workspace_max_bytes,
            max_output_bytes=self.max_output_bytes,
        )
        return self

    def require_transport(self, transport: RunnerWorkerTransport) -> None:
        transport.require_active()
        if transport.profile_id != self.profile.profile_id or transport.profile_revision != self.profile.revision:
            raise ValueError("Factory policy differs from administrator-approved private transport profile")


def capture_factory_pool(value: FactoryPoolSelection) -> dict[str, Any]:
    configuration = value.model_dump(mode="json")
    return {
        "mode": "pool",
        "configuration": configuration,
        "digest": pool_digest(configuration),
        "effective_network": "unverified",
        "enforced_controls": [],
        "reason": "Configured command pool policy; execution worker must verify the controller before execution",
    }


def validate_factory_pool(snapshot: dict[str, Any]) -> FactoryPoolSelection:
    value = FactoryPoolSelection.model_validate(snapshot.get("configuration"))
    if snapshot != capture_factory_pool(value):
        raise ValueError("Factory pool snapshot digest or metadata was changed")
    return value


class FactoryWorkerAdmin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    transport: RunnerWorkerTransport
    git_binary: Path

    _absolute = field_validator("git_binary")(absolute_path)


def load_factory_worker_configuration(path: Path) -> FactoryWorkerAdmin:
    if not path.is_absolute() or path.stem.startswith("pyfly") or path.suffix not in {".yaml", ".yml", ".toml"}:
        raise ValueError("Use an explicit private factory runner configuration")
    private_tls_file(path)
    return FactoryWorkerAdmin.model_validate(
        Config.from_file(path, load_defaults=False).effective_section("forge.runner.worker")
    )
