# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Strict, bounded recovery records; wire hashes are not Bridge request fingerprints."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from firefly_forge.configuration import validate_endpoint

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
RequestID = Annotated[str, Field(pattern=r"^fbr1_[a-f0-9]{32}$")]
Identifier = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> Any:
        if isinstance(value, dict) and "version" in value and type(value["version"]) is not int:
            raise ValueError("Record version must be an integer discriminator")
        return value


class RecoveryLimits(StrictRecord):
    admissions: Literal[1000]
    admissionBytes: Literal[4096]
    terminalBytes: Literal[262144]
    retainedBytes: Literal[16777216]

    @model_validator(mode="before")
    @classmethod
    def integer_limits(cls, value: Any) -> Any:
        if not isinstance(value, dict) or any(type(item) is not int for item in value.values()):
            raise ValueError("Recovery limits must be exact integers")
        return value


class RecoveryCapability(StrictRecord):
    version: Literal[1]
    principal_id: str = Field(min_length=1, max_length=64, pattern=r"^[^\x00-\x1f\x7f]+$")
    header: Literal["X-Bridge-Completion-Recovery"]
    value: Literal["1"]
    request_id_pattern: Literal["^fbr1_[a-f0-9]{32}$"]
    limits: RecoveryLimits


class CompletionDescriptor(StrictRecord):
    request_id: RequestID
    principal_id: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=200)
    endpoint: str = Field(min_length=1, max_length=2000)
    wire_sha256: Digest
    wire_bytes: int = Field(ge=1, le=9_000_000)
    image_revision: Digest | None = None

    @field_validator("endpoint")
    @classmethod
    def endpoint_valid(cls, value: str) -> str:
        if validate_endpoint(value) != value:
            raise ValueError("Recovery endpoint must be canonical")
        return value


class ObservationInput(StrictRecord):
    version: Literal[1] = 1
    observation_id: Identifier
    expected_intent_digest: Digest


def _uuid(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("Bridge job identity must be a canonical UUID")
    return value


class RemoteAdmission(StrictRecord):
    version: Literal[1]
    request_id: RequestID
    principal_id: str = Field(min_length=1, max_length=64)
    job_id: str
    model: str = Field(min_length=1, max_length=200)
    fingerprint: Digest
    fingerprint_algorithm: Literal["sha256-json-stringify-prepared-v1"]
    wire_sha256: Digest
    wire_bytes: int = Field(ge=1, le=9_000_000)
    image_revision: Digest | None
    created_at: str = Field(min_length=20, max_length=30)
    digest: Digest

    _job = field_validator("job_id")(_uuid)


class RemoteTerminal(StrictRecord):
    version: Literal[1]
    job_id: str
    admission_digest: Digest
    attempt_id: str | None
    status: Literal["succeeded", "failed", "canceled", "interrupted", "timed_out"]
    recorded_at: str = Field(min_length=20, max_length=30)
    result_json: str | None = Field(default=None, max_length=262144)
    result_sha256: Digest | None = None
    result_bytes: int | None = Field(default=None, ge=0, le=9007199254740991)
    reason: Literal["result_too_large", "invalid_completion"] | None = None
    digest: Digest

    _job = field_validator("job_id")(_uuid)

    @field_validator("attempt_id")
    @classmethod
    def attempt_uuid(cls, value: str | None) -> str | None:
        return _uuid(value) if value is not None else None


class RemoteSnapshot(StrictRecord):
    version: Literal[1]
    admission: RemoteAdmission
    state: Literal["pending", "paused", "terminal", "result_unavailable"]
    terminal: RemoteTerminal | None
    current_job_status: Literal[
        "queued",
        "running",
        "cancel_requested",
        "pause_requested",
        "paused",
        "succeeded",
        "failed",
        "canceled",
        "interrupted",
        "timed_out",
    ]
    prepared_at: str = Field(min_length=20, max_length=30)
    observations: dict[str, Any] | None


def sealed_record(data: dict[str, Any], fields: tuple[str, ...]) -> None:
    from datetime import datetime

    expected = data.get("digest")
    # Bridge seals a fixed insertion-ordered scalar record, independently of its request normalizer.
    record = {key: data[key] for key in fields if key in data}
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if expected != hashlib.sha256(encoded).hexdigest():
        raise ValueError("Bridge immutable record digest does not match")
    for key in ("created_at", "recorded_at", "prepared_at"):
        if key in data:
            timestamp = datetime.fromisoformat(data[key].replace("Z", "+00:00"))
            if timestamp.utcoffset() is None:
                raise ValueError("Bridge timestamp must include its timezone")


def validate_remote_snapshot(raw: dict[str, Any], descriptor: dict[str, Any]) -> dict[str, Any]:
    if len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode()) > 262144:
        raise ValueError("Bridge original-result envelope exceeds 256 KiB")
    parsed = RemoteSnapshot.model_validate(raw)
    admission = raw["admission"]
    sealed_record(admission, tuple(RemoteAdmission.model_fields)[:-1])
    for key in ("request_id", "principal_id", "model", "wire_sha256", "wire_bytes", "image_revision"):
        if admission[key] != descriptor.get(key):
            raise ValueError("Bridge admission does not match the retained completion intent")
    terminal = raw["terminal"]
    if terminal is None:
        if parsed.state not in {"pending", "paused"} or parsed.observations is None:
            raise ValueError("Bridge nonterminal state is inconsistent")
        observations = parsed.observations
        if set(observations) != {"reason", "retry_at"}:
            raise ValueError("Bridge observation fields are invalid")
        reason, retry = observations["reason"], observations["retry_at"]
        if (reason is not None and (not isinstance(reason, str) or len(reason) > 1000)) or (
            retry is not None and (type(retry) not in {int, float} or not 0 <= retry <= 8640000000000000)
        ):
            raise ValueError("Bridge observed reason or reset is invalid")
    else:
        sealed_record(terminal, tuple(RemoteTerminal.model_fields)[:-1])
        if terminal["job_id"] != admission["job_id"] or terminal["admission_digest"] != admission["digest"]:
            raise ValueError("Bridge terminal belongs to another admission")
        if parsed.state not in {"terminal", "result_unavailable"} or parsed.observations is not None:
            raise ValueError("Bridge terminal state is inconsistent")
        result = terminal.get("result_json")
        if result is not None:
            encoded = result.encode("utf-8")
            if (
                terminal.get("result_bytes") != len(encoded)
                or terminal.get("result_sha256") != hashlib.sha256(encoded).hexdigest()
            ):
                raise ValueError("Bridge original result bytes do not match their digest")
            if terminal["status"] != "succeeded" or terminal.get("reason") is not None or parsed.state != "terminal":
                raise ValueError("Bridge original success is inconsistent")
            completion = json.loads(result)
            if (
                not isinstance(completion, dict)
                or completion.get("model") != admission["model"]
                or completion.get("id") != "chatcmpl-" + admission["job_id"]
                or completion.get("object") != "chat.completion"
            ):
                raise ValueError("Bridge original completion does not bind its admitted job and model")
        elif terminal["status"] == "succeeded":
            if parsed.state != "result_unavailable" or terminal.get("reason") is None:
                raise ValueError("Bridge unavailable success needs its retained reason")
        elif parsed.state != "terminal" or any(key in terminal for key in ("result_sha256", "result_bytes", "reason")):
            raise ValueError("Bridge unsuccessful terminal cannot claim a result")
    return raw


class CompletionIntentPayload(StrictRecord):
    version: Literal[1]
    run_id: Identifier
    task_id: Literal["implement", "review"]
    project_id: Identifier
    repository_id: Identifier
    task_key: str = Field(min_length=1, max_length=64)
    config_field: Literal["config_revision", "review_config_revision"]
    config_revision: Identifier
    scope_digest: Digest
    profile_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    descriptor: CompletionDescriptor
    task_attempt: int = Field(ge=1, le=1000000)

    @model_validator(mode="after")
    def task_configuration(self) -> CompletionIntentPayload:
        if self.config_field != ("review_config_revision" if self.task_id == "review" else "config_revision"):
            raise ValueError("Completion task and frozen configuration role differ")
        return self


class CompletionObservationPayload(StrictRecord):
    version: Literal[1]
    request_id: RequestID
    intent_digest: Digest
    input: ObservationInput
    observed_by: Identifier
    snapshot: RemoteSnapshot


class CompletionResponsePayload(StrictRecord):
    version: Literal[1]
    request_id: RequestID
    intent_digest: Digest
    response: str = Field(max_length=262144)
    response_sha256: Digest
    response_bytes: int = Field(ge=0, le=262144)

    @model_validator(mode="after")
    def response_binding(self) -> CompletionResponsePayload:
        encoded = self.response.encode("utf-8")
        if len(encoded) != self.response_bytes or hashlib.sha256(encoded).hexdigest() != self.response_sha256:
            raise ValueError("Retained transport response does not match its byte digest")
        return self
