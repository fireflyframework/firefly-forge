# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One-use recovery of an admitted continuity request proven never worker-claimed."""

from typing import Any, Literal

from pydantic import Field, field_validator

from firefly_forge.bridge_completion_models import Digest, Identifier, StrictRecord


class InvalidReadRecoveryInputV1(StrictRecord):
    version: Literal[1]
    candidate_digest: Digest
    expected_admission_seal: Digest
    expected_failed_event_digest: Digest
    valid_for_seconds: int = Field(ge=1, le=7200)
    acknowledge_never_claimed: Literal[True]
    acknowledge_same_request_and_attempt: Literal[True]
    acknowledge_preserved_consumption_and_budgets: Literal[True]
    acknowledge_renewed_same_attempt_lease: Literal[True]

    @field_validator(
        "acknowledge_never_claimed",
        "acknowledge_same_request_and_attempt",
        "acknowledge_preserved_consumption_and_budgets",
        "acknowledge_renewed_same_attempt_lease",
        mode="before",
    )
    @classmethod
    def exact_acknowledgement(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("Explicitly acknowledge a never-claimed request without replay or new budget")
        return value


class InvalidReadRecoveryUsageV1(StrictRecord):
    requests: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    request_limit: int | None = Field(default=None, ge=0)
    tool_calls_limit: int | None = Field(default=None, ge=0)
    commands: int = Field(ge=0)
    command_limit: int | None = Field(default=None, ge=1)


type InvalidReadRecoveryHold = Literal[
    "current_profile_unqualified",
    "lease_policy_disabled",
    "unavailable_observation_policy_disabled",
    "active_execution_budget_expired",
]


class InvalidReadRecoveryCandidateV1(StrictRecord):
    version: Literal[1]
    kind: Literal["unclaimed_invalid_read_recovery_candidate"]
    run_id: Identifier
    request_id: Identifier
    digest: Digest
    admission: Literal["READY", "HOLD"]
    holds: list[InvalidReadRecoveryHold]
    execution_authorized: Literal[False]
    original_admission_seal: Digest
    failed_event_digest: Digest
    original_settlement_digest: Digest
    original_claim_digest: Digest
    checkpoint_digest: Digest
    source_digest: Digest
    attempt: int = Field(ge=1)
    current_profile: dict[str, str] | None
    current_runtime_contract: Digest
    policy_digest: Digest
    maximum_window_seconds: int = Field(ge=0, le=7200)
    frozen_timeout_seconds: int = Field(ge=1, le=7200)
    original_lease: dict[str, Any]
    usage: InvalidReadRecoveryUsageV1


class InvalidReadRecoveryStatusV1(StrictRecord):
    version: Literal[1]
    kind: Literal["unclaimed_invalid_read_recovery_status"]
    run_id: Identifier
    request_id: Identifier
    recovery_id: Identifier | None
    recovery_record_digest: Digest | None
    status: Literal["not_recovered", "queued", "executing", "completed", "failed", "needs_reconciliation"]
    original_admission_seal: Digest
    lease: dict[str, Any] | None
    current_profile: dict[str, str] | None


class InvalidReadRecoveryWitnessV1(StrictRecord):
    version: Literal[1]
    kind: Literal["unclaimed_invalid_read_recovery"]
    run_id: Identifier
    request_id: Identifier
    recovery_id: Identifier
    request: InvalidReadRecoveryInputV1
    actor: dict[str, Any]
    administrator: dict[str, Any]
    candidate_digest: Digest
    original_ticket_digest: Digest
    original_admission_seal: Digest
    original_request_digest: Digest
    original_settlement_digest: Digest
    original_claim_digest: Digest
    checkpoint_digest: Digest
    checkpoint_usage_digest: Digest
    failed_state: dict[str, Any]
    failed_event_digest: Digest
    request_events_digest: Digest
    post_admission_journal_digest: Digest
    run_digest: Digest
    task_digest: Digest
    source_digest: Digest
    binding: dict[str, Any]
    lease: dict[str, Any]
    lease_policy: dict[str, Any]
    policy_epoch: dict[str, Any] | None
    current_profile: dict[str, str]
    settings_digest: Digest
    protected_scope_digest: Digest
    command_custody_digest: Digest
    digest: Digest
