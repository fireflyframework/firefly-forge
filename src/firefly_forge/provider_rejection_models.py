# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit administrator review of a provider rejection before remote admission."""

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from firefly_forge.bridge_completion_models import Digest, RequestID, StrictRecord


class ProviderExecutionEpochV1(StrictRecord):
    executable_sha256: Digest
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_tree: str = Field(pattern=r"^[a-f0-9]{40}$")
    attestation_sha256: Digest


class ProviderValidationEpochV1(StrictRecord):
    executable_sha256: Digest
    source_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_tree: str = Field(pattern=r"^[a-f0-9]{40}$")
    validator_sha256: Digest
    route_sha256: Digest
    report_sha256: Digest
    binary_probe_report_sha256: Digest


class ReviewedProviderRejectionV1(StrictRecord):
    version: Literal[1]
    provider: Literal["bridge"]
    classification: Literal["request_refused_before_admission"]
    completion_request_id: RequestID
    intent_digest: Digest
    checkpoint_record_digest: Digest
    wire_sha256: Digest
    wire_bytes: int = Field(ge=1, le=9_000_000)
    endpoint: str = Field(min_length=1, max_length=2000)
    principal_id: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=200)
    executed_epoch: ProviderExecutionEpochV1
    validation_epoch: ProviderValidationEpochV1
    http_status: int = Field(ge=400, le=499)
    admissions: Literal[0]
    jobs: Literal[0]
    provider_dispatches: Literal[0]
    tool_callbacks: Literal[0]

    @field_validator("admissions", "jobs", "provider_dispatches", "tool_callbacks", mode="before")
    @classmethod
    def exact_zero(cls, value: Any) -> int:
        if type(value) is not int or value != 0:
            raise ValueError("Only an explicitly reviewed pre-admission refusal is supported")
        return value

    @model_validator(mode="after")
    def identical_source_epoch(self) -> "ReviewedProviderRejectionV1":
        if (
            self.executed_epoch.source_revision != self.validation_epoch.source_revision
            or self.executed_epoch.source_tree != self.validation_epoch.source_tree
            or self.executed_epoch.executable_sha256 != self.validation_epoch.executable_sha256
        ):
            raise ValueError("The reviewed validator must be from the exact executed provider source epoch")
        return self


class RejectedBoundaryRecoveryInputV1(StrictRecord):
    version: Literal[1]
    candidate_digest: Digest
    evidence: ReviewedProviderRejectionV1
    valid_for_seconds: int = Field(ge=1, le=7200)
    acknowledge_reviewed_provider_epoch: Literal[True]
    acknowledge_no_original_response_or_tool_replay: Literal[True]
    acknowledge_same_request_attempt_and_budgets: Literal[True]
    acknowledge_finite_ownership_lease: Literal[True]

    @field_validator(
        "acknowledge_reviewed_provider_epoch",
        "acknowledge_no_original_response_or_tool_replay",
        "acknowledge_same_request_attempt_and_budgets",
        "acknowledge_finite_ownership_lease",
        mode="before",
    )
    @classmethod
    def exact_acknowledgement(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("Explicitly review the rejection without replay or a new execution allowance")
        return value
