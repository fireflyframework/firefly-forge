# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Internal checkpoint envelopes; these records do not authorize graph restoration."""

from typing import Literal

from pydantic import Field

from firefly_forge.bridge_completion_models import Digest, RequestID, StrictRecord


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
