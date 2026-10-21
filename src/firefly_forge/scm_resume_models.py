# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Opt-in same-run resume contracts, separate from immutable SCM delegation V1."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract


class SCMResumeActorContextV1(StrictContract):
    """Internal, invocation-scoped authority; never an ordinary SCM binding."""

    version: Literal[1]
    transport: Literal["signed_resume_v2", "github_rest_poll_resume_v3"]
    action: Literal["resume"]
    verified_receipt_id: NativeID
    verified_reference_digest: Digest
    run_id: NativeID
    project_id: NativeID
    repository_id: NativeID
    component_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    native_user_id: NativeID
    binding_digest: Digest
    grant_digest: Digest


class SCMResumeGrantInputV2(StrictContract):
    version: Literal[2]
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
            raise ValueError("Explicitly consent to resuming the same answered clarification")
        return value

    @model_validator(mode="after")
    def unique_components(self) -> SCMResumeGrantInputV2:
        if len(set(self.component_ids)) != len(self.component_ids):
            raise ValueError("Resume component scope must be unique")
        return self


class SCMResumeCommandV2(StrictContract):
    version: Literal[2]
    action: Literal["resume"]
    run_id: NativeID
    expected_grant_digest: Digest
    expected_question_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
    expected_response_digest: Digest
    expected_cut_digest: Digest


class SCMResumeStatusV2(StrictContract):
    version: Literal[2]
    action: Literal["status"]
    run_id: NativeID
    expected_grant_digest: Digest


class SCMResumeGrantReceiptV2(StrictContract):
    version: Literal[2]
    event_id: NativeID
    project_id: NativeID
    repository_id: NativeID
    binding_key: Digest
    native_user_id: NativeID
    revision: int = Field(ge=1, le=50)
    previous_digest: Digest | None
    prepared_at: str = Field(min_length=1, max_length=50)
    administering_user_id: NativeID
    request: SCMResumeGrantInputV2
    request_digest: Digest
    digest: Digest
