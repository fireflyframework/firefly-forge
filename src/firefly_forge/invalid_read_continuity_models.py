# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Inspection records do not authorize a consumed graph or extend its lease."""

from typing import Any, Literal

from pydantic import Field, model_validator

from firefly_forge.agent_checkpoint_models import InvocationBinding
from firefly_forge.bridge_completion_models import Digest, Identifier, RequestID, StrictRecord


class InvalidReadCompletionRefV1(StrictRecord):
    request_id: RequestID
    intent_digest: Digest
    checkpoint_record_digest: Digest
    response_record_digest: Digest
    consume_record_digest: Digest


class InvalidReadProofV1(StrictRecord):
    version: Literal[1]
    kind: Literal["consumed_invalid_read_continuity"]
    run_id: Identifier
    task_key: str = Field(min_length=1, max_length=64)
    attempt: int = Field(ge=1, le=1000000)
    hold_request_id: RequestID
    completion_request_id: RequestID
    intent_digest: Digest
    checkpoint_record_digest: Digest
    checkpoint_digest: Digest
    response_record_digest: Digest
    consume_record_digest: Digest
    completions: list[InvalidReadCompletionRefV1] = Field(min_length=1, max_length=100)
    journal_digest: Digest
    run_digest: Digest
    task_digest: Digest
    policy_digest: Digest
    runtime_contract: Digest
    tool_call_id: str = Field(min_length=1, max_length=200)
    tool_arguments_digest: Digest
    deadline_ms: int = Field(ge=1)
    result: dict[Literal["status"], Literal["invalid_query"]] = Field(min_length=1, max_length=1)
    admission: Literal["HOLD"]
    holds: list[Literal["continuation_lease_policy_unavailable", "original_deadline_expired"]]
    digest: Digest


class InvalidReadAdmissionInputV1(StrictRecord):
    version: Literal[1]
    candidate_digest: Digest
    expected_run_digest: Digest
    expected_journal_digest: Digest
    expected_policy_digest: Digest
    valid_for_seconds: int = Field(ge=1, le=7200)
    acknowledge_no_new_grant: Literal[True]
    acknowledge_original_consumption: Literal[True]
    acknowledge_renewed_same_attempt_lease: Literal[True]

    @model_validator(mode="before")
    @classmethod
    def exact_acknowledgements(cls, value: Any) -> Any:
        if not isinstance(value, dict) or any(
            value.get(key) is not True
            for key in (
                "acknowledge_no_new_grant",
                "acknowledge_original_consumption",
                "acknowledge_renewed_same_attempt_lease",
            )
        ):
            raise ValueError("Continuity acknowledgements require exact boolean true")
        return value


class InvalidReadSettlementV1(StrictRecord):
    version: Literal[1]
    request_id: Identifier
    proof: InvalidReadProofV1
    policy_epoch: dict[str, Any] | None
    request: InvalidReadAdmissionInputV1
    lease: dict[str, int | None]
    lease_policy: dict[str, Any]
    frozen_timeout_seconds: int = Field(ge=1, le=7200)
    candidate_digest: Digest
    reconstruction: dict[str, Any]
    current_profile: dict[str, str]
    actor: dict[str, Any]
    original_binding: InvocationBinding
    binding: InvocationBinding
    observed_hold: dict[str, Any]
    checkpoint_digest: Digest
    command_custody_digest: Digest
    settings_digest: Digest
    protected_scope_digest: Digest
    digest: Digest


class InvalidReadClaimV1(StrictRecord):
    version: Literal[1]
    request_id: Identifier
    task_key: str = Field(min_length=1, max_length=64)
    attempt: int = Field(ge=1, le=1000000)
    settlement_digest: Digest
    lease_generation: Identifier
    expires_at_ms: int = Field(ge=1, le=2**53 - 1)
