# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Only a retained configured-check index and request identity reach the coding tool."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from firefly_forge.models import Command

CheckIndex = Annotated[int, Field(ge=0, le=19, strict=True)]


class ConfiguredCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    command_index: CheckIndex
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class CodingCheckGrantInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    recovery_request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    candidate_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_revision: int = Field(ge=0)
    command_indices: list[CheckIndex] = Field(min_length=1, max_length=20)
    call_limit: int = Field(ge=1, le=20)
    valid_seconds: int = Field(ge=30, le=3600)
    bind_port: int = Field(default=0, ge=0, le=65535)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("bind_port")
    @classmethod
    def loopback_port(cls, value: int) -> int:
        if value != 0 and value < 1024:
            raise ValueError("Use an ephemeral port or an explicitly reviewed unprivileged loopback port")
        return value

    @field_validator("command_indices")
    @classmethod
    def distinct_indices(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("Select each configured check at most once")
        return value

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Explain the explicit configured-check grant")
        return value


class CodingCheckRevocationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    grant_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)


class ConfiguredCheckReview(Command):
    command_index: CheckIndex


class CodingCheckChannel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    transport: Literal["streamable_http"]
    host: Literal["127.0.0.1"]
    bind_port: int = Field(ge=0, le=65535)


class CodingCheckCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1]
    protocol: Literal["native-coding-configured-check-v1"]
    run_id: str
    agent: Literal["codex", "claude"]
    recovery_request_id: str
    task_key: str
    task_attempt: int
    revision: int
    actor_digest: str
    cut: dict[str, Any]
    source_digest: str
    command_indices: list[CheckIndex]
    command_count: int
    commands: list[ConfiguredCheckReview]
    runner: dict[str, Any]
    channel: CodingCheckChannel
    independent_final_check_count: int
    shared_command_limit: int
    shared_commands_used: int
    shared_commands_remaining: int
    interactive_commands_remaining: int
    journal_digest: str
    digest: str


class CodingCheckGrantReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1]
    protocol: Literal["native-coding-configured-check-v1"]
    request: CodingCheckGrantInput
    actor: dict[str, Any]
    candidate: CodingCheckCandidate
    revision: int
    call_limit: int
    expires_at_ms: int
    receipt_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class CodingCheckCurrentStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    protocol: Literal["native-coding-configured-check-v1"]
    run_id: str
    grant_id: str | None
    revision: int
    status: Literal["none", "active", "revoked", "expired"]
    receipt_digest: str | None
    expires_at_ms: int | None


class CodingCheckRevocationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1]
    protocol: Literal["native-coding-configured-check-v1"]
    request: CodingCheckRevocationInput
    actor: dict[str, Any]
    revision: int


def configured_command(commands: list[dict[str, Any]], allowed: list[int], request: ConfiguredCheckRequest) -> Command:
    if request.command_index not in allowed or request.command_index >= len(commands):
        raise ValueError("The check index is outside the granted configured scope")
    return Command.model_validate(commands[request.command_index]).model_copy(deep=True)
