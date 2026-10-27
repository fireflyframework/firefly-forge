# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Inspection records do not authorize a consumed graph or extend its lease."""

from typing import Any, Literal, cast

from pydantic import Field, TypeAdapter, model_validator

from firefly_forge.agent_checkpoint_models import InvocationBinding
from firefly_forge.bridge_completion_models import Digest, Identifier, RequestID, StrictRecord


class InvalidReadCompletionRefV1(StrictRecord):
    request_id: RequestID
    intent_digest: Digest
    checkpoint_record_digest: Digest
    response_record_digest: Digest
    consume_record_digest: Digest


class _InvalidReadProofFields(StrictRecord):
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


class UnavailableSourceObservationV1(StrictRecord):
    version: Literal[1]
    index: int = Field(ge=0)
    tool_call_id: str = Field(min_length=1, max_length=200)
    tool_name: Literal["list_files"]
    arguments_digest: Digest
    response_sha256: Digest
    source_digest: Digest
    grants_digest: Digest
    handler_sha256: Digest
    original_outcome: Literal["not_retained"]
    outcome: dict[str, str]

    @model_validator(mode="after")
    def exact_negative_outcome(self) -> "UnavailableSourceObservationV1":
        if self.outcome != {"status": "observation_unavailable", "reason": "original_read_result_not_retained"}:
            raise ValueError("Unavailable source observations cannot become successful historical results")
        return self


class InvalidReadProofV1(_InvalidReadProofFields):
    version: Literal[1]


class InvalidReadProofV2(_InvalidReadProofFields):
    version: Literal[2]
    unavailable_observations: list[UnavailableSourceObservationV1] = Field(min_length=1)
    native_tool_custody_digest: Digest


type InvalidReadProof = InvalidReadProofV1 | InvalidReadProofV2
PROOF = TypeAdapter[InvalidReadProof](cast(Any, InvalidReadProof))


class _InvalidReadAdmissionFields(StrictRecord):
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


class InvalidReadAdmissionInputV1(_InvalidReadAdmissionFields):
    version: Literal[1]


class InvalidReadAdmissionInputV2(_InvalidReadAdmissionFields):
    version: Literal[2]
    acknowledge_unavailable_source_observations: Literal[True]

    @model_validator(mode="before")
    @classmethod
    def explicit_observation_acknowledgement(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("acknowledge_unavailable_source_observations") is not True:
            raise ValueError("Explicitly acknowledge the unavailable original source observations")
        return value


type InvalidReadAdmissionInput = InvalidReadAdmissionInputV1 | InvalidReadAdmissionInputV2
ADMISSION = TypeAdapter[InvalidReadAdmissionInput](cast(Any, InvalidReadAdmissionInput))


class _InvalidReadSettlementFields(StrictRecord):
    request_id: Identifier
    policy_epoch: dict[str, Any] | None
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


class InvalidReadSettlementV1(_InvalidReadSettlementFields):
    version: Literal[1]
    proof: InvalidReadProofV1
    request: InvalidReadAdmissionInputV1


class InvalidReadSettlementV2(_InvalidReadSettlementFields):
    version: Literal[2]
    proof: InvalidReadProofV2
    request: InvalidReadAdmissionInputV2


type InvalidReadSettlement = InvalidReadSettlementV1 | InvalidReadSettlementV2
SETTLEMENT_RECORD = TypeAdapter[InvalidReadSettlement](cast(Any, InvalidReadSettlement))


class InvalidReadClaimV1(StrictRecord):
    version: Literal[1]
    request_id: Identifier
    task_key: str = Field(min_length=1, max_length=64)
    attempt: int = Field(ge=1, le=1000000)
    settlement_digest: Digest
    lease_generation: Identifier
    expires_at_ms: int = Field(ge=1, le=2**53 - 1)
