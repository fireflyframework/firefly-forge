# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded process leases and conditions evaluated without model polling."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from firefly_forge.sandbox_models import bounded_argv
from firefly_forge.workspace_tools import _private_name


class MonitorStartInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    argv: list[str] = Field(min_length=1, max_length=64)
    timeout_seconds: int | None = Field(default=None, ge=1)

    _argv = field_validator("argv")(bounded_argv)


class ExitCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["exit"]


class OutputCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["output"]
    text: str = Field(min_length=1, max_length=4096)


class FileCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["file"]
    path: str = Field(min_length=1, max_length=1024)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("path")
    @classmethod
    def confined_path(cls, value: str) -> str:
        from pathlib import PurePosixPath

        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or not path.parts
            or ".." in path.parts
            or "\\" in value
            or "\x00" in value
            or path.as_posix() != value
            or any(_private_name(part) for part in path.parts)
        ):
            raise ValueError("File waits require a public relative workspace path")
        return value


WaitCondition = Annotated[ExitCondition | OutputCondition | FileCondition, Field(discriminator="kind")]


class MonitorWaitInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    monitor_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    timeout_seconds: int | None = Field(default=None, ge=1)
    condition: WaitCondition


class MonitorObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    monitor_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    owner_epoch: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    status: Literal["running", "settling", "exited", "cancelled", "timed_out", "output_limit", "needs_reconciliation"]
    issued_at_ms: int | None = Field(default=None, ge=1)
    ownership_expires_at_ms: int | None = Field(default=None, ge=1)
    work_deadline_ms: int | None = Field(default=None, ge=1)
    exit_code: int | None = None
    output_bytes: int | None = Field(default=None, ge=0)
    output_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    output_truncated: bool = False


class MonitorWaitEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    condition: WaitCondition
    outcome: Literal["satisfied", "timeout", "process_ended", "unavailable"]
    observation: MonitorObservation
    file_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
