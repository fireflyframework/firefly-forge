# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Internal checkpoint envelopes; these records do not authorize graph restoration."""

from typing import Literal

from pydantic import Field, model_validator

from firefly_forge.bridge_completion_models import Digest, Identifier, RequestID, StrictRecord


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
    grants: list[str] = Field(max_length=6)
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
