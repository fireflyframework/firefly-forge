# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Internal checkpoint envelopes; these records do not authorize graph restoration."""

from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from firefly_forge.bridge_completion_models import Digest, Identifier, RequestID, StrictRecord
from firefly_forge.work_duration_models import WorkDurationPolicyV2


class CheckpointPayload(StrictRecord):
    version: Literal[1]
    request_id: RequestID
    intent_digest: Digest
    checkpoint_digest: Digest
    snapshot: str = Field(min_length=1, max_length=196608)


class ConsumePayload(StrictRecord):
    version: Literal[1]
    request_id: RequestID
    intent_digest: Digest
    checkpoint_record_digest: Digest
    response_record_digest: Digest
    response_sha256: Digest
    response_bytes: int = Field(ge=1, le=262144)


class InvocationBinding(StrictRecord):
    invocation_id: Identifier
    ordinal: int = Field(ge=1, le=100)
    predecessor: Digest | None
    brief: str = Field(min_length=1, max_length=100000)
    backend: str = Field(min_length=1, max_length=32)
    frontend: str = Field(min_length=1, max_length=32)
    round: int = Field(ge=1, le=1000000)
    deadline_ms: int = Field(ge=1, le=2**53 - 1)
    grants: list[str] = Field(max_length=9)
    contract: Digest
    run_cut: Digest
    workspace: dict[str, str] = Field(min_length=4, max_length=4)


class CheckpointPayloadV2(StrictRecord):
    version: Literal[2]
    request_id: RequestID
    intent_digest: Digest
    checkpoint_digest: Digest
    snapshot: str = Field(min_length=1, max_length=196608)
    invocation: InvocationBinding


class OwnedInvocationBindingV3(StrictRecord):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    protocol: Literal["forge-owned-checkpoint-v3"]
    invocation_id: Identifier
    ordinal: int = Field(ge=1, le=100)
    predecessor: Digest | None
    brief: str = Field(min_length=1, max_length=100000)
    backend: str = Field(min_length=1, max_length=32)
    frontend: str = Field(min_length=1, max_length=32)
    round: int = Field(ge=1, le=1000000)
    work_duration: WorkDurationPolicyV2
    work_started_at_ms: int = Field(ge=1)
    work_deadline_ms: int | None = Field(ge=1)
    grants: list[str] = Field(max_length=9)
    contract: Digest
    run_cut: Digest
    workspace: dict[str, str] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def exact_work_deadline(self) -> "OwnedInvocationBindingV3":
        timeout = self.work_duration.timeout_seconds
        expected = None if timeout is None else self.work_started_at_ms + timeout * 1000
        if self.work_deadline_ms != expected:
            raise ValueError("Owned checkpoint deadline must retain its exact original work policy and start")
        return self


class CheckpointPayloadV3(StrictRecord):
    version: Literal[3]
    request_id: RequestID
    intent_digest: Digest
    checkpoint_digest: Digest
    snapshot: str = Field(min_length=1, max_length=196608)
    invocation: OwnedInvocationBindingV3


InvocationContext = InvocationBinding | OwnedInvocationBindingV3
InvocationCheckpoint = CheckpointPayloadV2 | CheckpointPayloadV3


class ResultReference(StrictRecord):
    kind: Literal["http_response", "bridge_observation"]
    record_digest: Digest
    observation_id: Identifier | None = None
    terminal_digest: Digest | None = None

    @model_validator(mode="after")
    def source_fields(self) -> "ResultReference":
        if (self.observation_id is not None and self.terminal_digest is not None) != (
            self.kind == "bridge_observation"
        ):
            raise ValueError("Original result reference requires exactly its source fields")
        if self.kind == "http_response" and (self.observation_id is not None or self.terminal_digest is not None):
            raise ValueError("HTTP result cannot be relabeled as remote observation")
        return self


class ConsumePayloadV2(StrictRecord):
    version: Literal[2]
    request_id: RequestID
    intent_digest: Digest
    checkpoint_record_digest: Digest
    source: ResultReference
    response_sha256: Digest
    response_bytes: int = Field(ge=1, le=262144)
    restore_request_id: Identifier
    generation: Identifier


class RestoreInput(StrictRecord):
    request_id: Identifier
    expected_checkpoint_digest: Digest
    expected_intent_digest: Digest
    source: ResultReference
