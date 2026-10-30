# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native operational projections are versioned evidence, never authored user facts."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firefly_forge.learning_models import LearningEnvelope, LearningProjectionObservationReceipt, State


class OperationalEvidenceV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    version: Literal[2] = 2
    kind: Literal["local_outcome", "observed_failure"]
    authority: Literal["native_receipt_observation"] = "native_receipt_observation"
    canonical_memory: Literal[False] = False
    failure_cause: Literal["not_classified"] | None = None
    record_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    envelope: LearningEnvelope
    envelope_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: datetime

    @model_validator(mode="after")
    def exact_observation(self) -> "OperationalEvidenceV2":
        if self.envelope.type != self.kind or self.expires_at.utcoffset() is None:
            raise ValueError("Operational evidence requires its exact outcome kind and aware expiry")
        if (self.failure_cause is not None) != (self.kind == "observed_failure"):
            raise ValueError("Observed failures must retain their unclassified cause")
        return self


class NativeLearningVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    asset_id: str = Field(min_length=1, max_length=64)
    native_source_id: str = Field(min_length=1, max_length=64)
    source_version_id: str = Field(min_length=1, max_length=64)
    extraction_id: str = Field(min_length=1, max_length=64)


class NativeLearningCitation(NativeLearningVersion):
    source_id: str = Field(min_length=1, max_length=64)
    chunk_id: str = Field(min_length=1, max_length=200)
    workspace_id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)


class NativeLearningObservationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    version: Literal[2] = 2
    event_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    record_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    repository_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    kind: Literal["projection_observation_v2"] = "projection_observation_v2"
    event_state: Literal["verified"] = "verified"
    current_record_state: State
    current_projection_state: Literal["pending", "executing", "source_admitted", "verified", "unknown", "abandoned"]
    actor_id: str | None = Field(pattern=r"^[a-f0-9]{32}$")
    projection_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    submission_request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    workspace_id: str = Field(min_length=1, max_length=64)
    target_revision: str = Field(pattern=r"^[a-f0-9]{32}$")
    payload_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_id: str = Field(min_length=1, max_length=64)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_versions: tuple[NativeLearningVersion, ...] = Field(min_length=1)
    citations: tuple[NativeLearningCitation, ...] = Field(min_length=1)
    upstream_authority: Literal["unverified"] = "unverified"
    created_at: str = Field(min_length=1, max_length=64)
    sequence: int = Field(ge=1, strict=True)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


LearningProjectionReceipt = LearningProjectionObservationReceipt | NativeLearningObservationReceipt
