# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""API-observed GitHub identity is separate from immutable signed-webhook V1."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from firefly_forge.scm_authority_models import Digest, NativeID, ProviderIdentity, StrictContract, exact_origin

Component = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]


class PollContract(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class PollConfigurationV1(PollContract):
    version: Literal[1]
    request_id: NativeID
    expected_previous_digest: Digest | None
    expected_project_binding_digest: Digest
    expected_connection_revision: NativeID
    enabled: bool
    interval_seconds: int = Field(default=60, ge=60, le=3600)


class GitHubPollReferenceV1(ProviderIdentity):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    transport: Literal["github_rest_poll_v1"]
    provider: Literal["github"]
    web_origin: str = Field(max_length=500)
    connection_revision: NativeID
    connection_scope_digest: Digest
    resource_id: str = Field(pattern=r"^[1-9][0-9]{0,39}$")
    issue_number: int = Field(ge=1, strict=True)
    resource_updated_at: str = Field(min_length=1, max_length=50)
    payload_digest: Digest
    response_sha256: Digest
    observed_at: str = Field(min_length=1, max_length=50)

    @field_validator("web_origin")
    @classmethod
    def canonical_origin(cls, value: str) -> str:
        return exact_origin(value)


class SCMPollActorBindingInputV2(PollContract):
    version: Literal[2]
    request_id: NativeID
    expected_previous_digest: Digest | None
    expected_project_binding_digest: Digest
    observed_receipt_id: NativeID
    expected_reference_digest: Digest
    native_user_id: NativeID
    component_ids: list[Component] = Field(min_length=1, max_length=50)
    allowed_actions: list[Literal["respond", "status"]] = Field(min_length=1, max_length=2)
    active: bool
    acknowledge_api_observed_identity: Literal[True]

    @field_validator("acknowledge_api_observed_identity", mode="before")
    @classmethod
    def exact_ack(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Explicitly acknowledge the API-observed GitHub account")
        return value

    @model_validator(mode="after")
    def unique_scope(self) -> SCMPollActorBindingInputV2:
        if len(set(self.component_ids)) != len(self.component_ids) or len(set(self.allowed_actions)) != len(
            self.allowed_actions
        ):
            raise ValueError("Components and actions must be unique")
        return self


class SCMPollActorBindingReceiptV2(PollContract):
    version: Literal[2]
    event_id: NativeID
    project_id: NativeID
    repository_id: NativeID
    project_binding_digest: Digest
    binding_key: Digest
    revision: int = Field(ge=1, le=50, strict=True)
    previous_digest: Digest | None
    prepared_at: str = Field(min_length=1, max_length=50)
    administering_user_id: NativeID
    request: SCMPollActorBindingInputV2
    request_digest: Digest
    verified_reference: GitHubPollReferenceV1
    digest: Digest


class PollResponseProvenanceV1(ProviderIdentity):
    provider: Literal["github"]
    transport: Literal["github_rest_poll_v1"]
    binding_request_id: NativeID
    binding_digest: Digest
    verified_receipt_id: NativeID
    connection_revision: NativeID


class SCMRespondingActorV3(PollContract):
    version: Literal[3]
    user_id: NativeID
    authentication_method: Literal["scm_mapping"]
    provenance: PollResponseProvenanceV1


class PollPointer(PollContract):
    event_id: NativeID
    digest: Digest


class PollScopedPointer(PollPointer):
    project_id: NativeID


class PollIssue(PollContract):
    pull_request: bool


class PollCursor(PollContract):
    epoch: NativeID
    phase: Literal["issues", "comments"]
    page: int = Field(ge=1, le=20)
    pages: int = Field(ge=0, le=20)
    bytes: int = Field(ge=0, le=16 * 1024 * 1024)
    issues: dict[Annotated[str, Field(pattern=r"^[1-9][0-9]{0,39}$")], PollIssue] = Field(max_length=1000)
    ready: bool
    sequence: int = Field(ge=0, le=2**63 - 1)
    conflicts: int = Field(ge=0, le=2**63 - 1)
    retry_at: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None
    next_due_at: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None
    last_success_at: Annotated[str, Field(min_length=1, max_length=50)] | None
    last_error: Literal["poll_unavailable", "provider_backoff"] | None


class PollState(PollContract):
    version: Literal[1]
    configuration: PollScopedPointer | None
    config_requests: dict[NativeID, PollPointer] = Field(max_length=5000)
    heads: dict[Digest, PollScopedPointer] = Field(max_length=100)
    requests: dict[Annotated[str, Field(pattern=r"^[a-f0-9]{32}:[a-f0-9]{32}$")], PollPointer] = Field(max_length=5000)
    cursor: PollCursor | None
