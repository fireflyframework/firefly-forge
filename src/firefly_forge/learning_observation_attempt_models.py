# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Strict, separately namespaced observation attempts and exact local readback."""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def observation_attempt_id(value: str) -> str:
    if not re.fullmatch(r"obs2_[a-f0-9]{32}", value):
        raise ValueError("An exact v2 observation attempt identifier is required")
    return value


class LearningObservationAttemptInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    attempt_id: str = Field(pattern=r"^obs2_[a-f0-9]{32}$")
    source_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,200}$")
    projection_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    submission_request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    workspace_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,200}$")
    target_revision: str = Field(pattern=r"^[a-f0-9]{32}$")
    payload_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class LearningObservationAttemptEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    event_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class LearningObservationAttemptReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Literal[2] = 2
    attempt_id: str = Field(pattern=r"^obs2_[a-f0-9]{32}$")
    record_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    repository_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    project_id: str | None = Field(pattern=r"^[a-f0-9]{32}$")
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    actor_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    projection_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    submission_request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    workspace_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,200}$")
    target_revision: str = Field(pattern=r"^[a-f0-9]{32}$")
    payload_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: Literal["preparing", "dispatched", "not_dispatched", "verified", "unknown"]
    disposition: Literal["local_admission_refused", "verified"] | None
    created_at: str = Field(min_length=1, max_length=64)
    finished_at: str | None = Field(default=None, min_length=1, max_length=64)
    observation: LearningObservationAttemptEvent | None = None
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def exact_outcome(self) -> "LearningObservationAttemptReceipt":
        for value in (self.created_at, self.finished_at):
            if value is not None and datetime.fromisoformat(value).utcoffset() is None:
                raise ValueError("Observation attempt timestamps require an explicit timezone")
        if self.state == "not_dispatched":
            valid = (
                self.disposition == "local_admission_refused"
                and self.finished_at is not None
                and self.observation is None
            )
        elif self.state == "verified":
            valid = self.disposition == "verified" and self.finished_at is not None and self.observation is not None
        else:
            valid = (
                self.disposition is None
                and self.observation is None
                and (self.finished_at is not None if self.state == "unknown" else self.finished_at is None)
            )
        if not valid:
            raise ValueError("Observation attempt disposition does not match its retained state")
        return self
