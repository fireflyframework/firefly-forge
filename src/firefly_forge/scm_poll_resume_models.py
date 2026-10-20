# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Separate opt-in continuation authority for API-observed GitHub accounts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract


class SCMPollResumeGrantInputV3(StrictContract):
    version: Literal[3]
    transport: Literal["github_rest_poll_v1"]
    request_id: NativeID
    expected_previous_digest: Digest | None
    expected_project_binding_digest: Digest
    expected_binding_request_id: NativeID
    expected_binding_digest: Digest
    component_ids: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]] = Field(min_length=1, max_length=50)
    allowed_actions: list[Literal["resume"]] = Field(min_length=1, max_length=1)
    active: bool
    acknowledge_same_run_resume: Literal[True]

    @field_validator("acknowledge_same_run_resume", mode="before")
    @classmethod
    def explicit_consent(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Explicitly consent to same-run continuation through GitHub polling")
        return value

    @model_validator(mode="after")
    def unique_components(self) -> SCMPollResumeGrantInputV3:
        if len(set(self.component_ids)) != len(self.component_ids):
            raise ValueError("Polling resume component scope must be unique")
        return self


class SCMPollResumeGrantReceiptV3(StrictContract):
    version: Literal[3]
    event_id: NativeID
    project_id: NativeID
    repository_id: NativeID
    binding_key: Digest
    native_user_id: NativeID
    revision: int = Field(ge=1, le=50)
    previous_digest: Digest | None
    prepared_at: str = Field(min_length=1, max_length=50)
    administering_user_id: NativeID
    request: SCMPollResumeGrantInputV3
    request_digest: Digest
    digest: Digest
