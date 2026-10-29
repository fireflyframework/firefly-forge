# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit consent to accepting a final result whose response was already consumed."""

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract


class FinalOutcomeContinuationInputV1(StrictContract):
    version: Literal[1]
    candidate_digest: Digest
    valid_for_seconds: int = Field(ge=1, le=7200, strict=True)
    acknowledge_already_consumed_final_output: Literal[True]
    acknowledge_no_completed_effect_replay: Literal[True]
    acknowledge_same_request_attempt_and_budgets: Literal[True]
    acknowledge_finite_ownership_lease: Literal[True]

    @field_validator(
        "acknowledge_already_consumed_final_output",
        "acknowledge_no_completed_effect_replay",
        "acknowledge_same_request_attempt_and_budgets",
        "acknowledge_finite_ownership_lease",
        mode="before",
    )
    @classmethod
    def exact_acknowledgment(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError("Review the existing final result and preserved execution boundaries")
        return value


class FinalOutcomeCandidateV1(StrictContract):
    version: Literal[1]
    kind: Literal["final_outcome_continuation_candidate"]
    run_id: NativeID
    request_id: NativeID
    admission: Literal["READY", "HOLD"]
    holds: list[str]
    execution_authorized: Literal[False]
    original_admission_seal: Digest
    attempt: int = Field(ge=1, strict=True)
    ticket_digest: Digest
    events_digest: Digest
    journal_digest: Digest
    run_digest: Digest
    task_digest: Digest
    checkpoint_record_digest: Digest
    response_record_digest: Digest
    consume_record_digest: Digest
    report_digest: Digest
    final_outcome_digest: Digest
    source_digest: Digest
    current_runtime_contract: Digest
    current_profile: dict[str, Any] | None
    policy_digest: Digest
    maximum_window_seconds: int = Field(ge=1, strict=True)
    admission_window_purpose: Literal["claim_only"]
    outcome: Literal["complete", "blocked", "clarification", "failed"]
    followup_kind: str | None
    usage: dict[str, Any]
    limits: dict[str, Any]
    digest: Digest


class FinalOutcomeStatusV1(StrictContract):
    version: Literal[1]
    kind: Literal["final_outcome_continuation_status"]
    run_id: NativeID
    request_id: NativeID
    status: Literal["not_recovered", "queued", "executing", "completed", "failed", "needs_reconciliation"]
    action_id: NativeID | None
    witness_digest: Digest | None
    original_admission_seal: Digest
    lease: dict[str, Any] | None
    run_status: str


class RetainedVerifiedSourceV1(StrictContract):
    version: Literal[1]
    kind: Literal["retained_verified_final_source"]
    run_id: NativeID
    request_id: NativeID
    attempt: int = Field(ge=2, strict=True)
    parent_attempt: int = Field(ge=1, strict=True)
    task_key: NativeID
    final_outcome_witness_digest: Digest
    terminal_receipt_digest: Digest
    verification_custody_digest: Digest
    handoff_intent_digest: Digest
    handoff_outcome_digest: Digest
    continuation_intent_digest: Digest
    source_digest: Digest
    commands_digest: Digest
    sandbox_digest: Digest
    scope_digest: Digest
    digest: Digest

    @model_validator(mode="after")
    def exact_source(self) -> "RetainedVerifiedSourceV1":
        from firefly_forge.journal import digest

        if self.attempt != self.parent_attempt + 1 or digest(self.model_dump(exclude={"digest"})) != self.digest:
            raise ValueError("Retained source differs from its exact native verification continuation")
        return self
