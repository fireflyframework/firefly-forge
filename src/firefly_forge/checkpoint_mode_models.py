# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native consent to stronger recovery modes; no new model or execution budget."""

from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from firefly_forge.journal import digest
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract
from firefly_forge.unknown_command_models import NativeAcknowledgingActorV1


class CheckpointModeAdoptionInput(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    candidate_digest: Digest
    policy_digest: Digest
    operator_observation_digest: Digest
    observation_provenance: Literal["native_operator_pre_dispatch_observation"]
    acknowledge_known_pre_dispatch_failure: Literal[True]
    acknowledge_no_external_model_dispatch: Literal[True]
    acknowledge_original_intents_not_completed: Literal[True]
    acknowledge_only_stronger_modes: Literal[True]
    acknowledge_spent_history_and_budgets_preserved: Literal[True]
    acknowledge_same_native_continuation: Literal[True]
    acknowledge_existing_unknown_command_risk: Literal[True]

    @field_validator(
        "acknowledge_known_pre_dispatch_failure",
        "acknowledge_no_external_model_dispatch",
        "acknowledge_original_intents_not_completed",
        "acknowledge_only_stronger_modes",
        "acknowledge_spent_history_and_budgets_preserved",
        "acknowledge_same_native_continuation",
        "acknowledge_existing_unknown_command_risk",
        mode="before",
    )
    @classmethod
    def literal_consent(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError("Explicit native operator acknowledgement is required")
        return value


class CheckpointModeProofV1(StrictContract):
    version: Literal[1]
    run_id: NativeID
    task_key: NativeID
    attempt: int = Field(ge=1)
    original_config_revision: NativeID
    original_settings_digest: Digest
    historical_custody_digest: Digest
    required_settings_digest: Digest
    implementation_intent_digest: Digest
    continuation_intent_digest: Digest
    continuation_key: str = Field(min_length=1)
    provider_digest: Digest
    journal_digest: Digest
    original_failure: Literal["native_agentic_checkpoint_wal_required", "bridge_completion_wal_required"]
    frozen_budget_digest: Digest
    startup_recovery_digest: Digest | None
    original_task: dict[str, Any]
    digest: Digest

    @model_validator(mode="after")
    def exact_proof(self) -> "CheckpointModeProofV1":
        if digest(self.model_dump(exclude={"digest"})) != self.digest:
            raise ValueError("Checkpoint mode proof digest changed")
        return self


class CheckpointModeAckBindingV1(StrictContract):
    execution_id: NativeID
    execution_digest: Digest
    record_digest: Digest
    old_policy_digest: Digest
    actor: NativeAcknowledgingActorV1


class CheckpointModeActorV1(NativeAcknowledgingActorV1):
    identity_digest: Digest


class CheckpointModeCandidateV1(StrictContract):
    version: Literal[1]
    run_id: NativeID
    actor: CheckpointModeActorV1
    policy_digest: Digest
    acknowledgement_policy_digest: Digest
    run_digest: Digest
    source_digest: Digest
    verification_custody_digest: Digest
    original_scope_digest: Digest
    proof: CheckpointModeProofV1
    acknowledgements: list[CheckpointModeAckBindingV1]
    digest: Digest

    @model_validator(mode="after")
    def exact_candidate(self) -> "CheckpointModeCandidateV1":
        if self.proof.run_id != self.run_id or digest(self.model_dump(exclude={"digest"})) != self.digest:
            raise ValueError("Checkpoint mode candidate digest changed")
        return self


class CheckpointModeReceiptV1(StrictContract):
    version: Literal[1]
    run_id: NativeID
    request_id: NativeID
    candidate: CheckpointModeCandidateV1
    request: CheckpointModeAdoptionInput
    adoption_intent_digest: Digest
    execution_config_revision: NativeID

    @model_validator(mode="after")
    def exact_receipt(self) -> "CheckpointModeReceiptV1":
        if (
            self.run_id != self.candidate.run_id
            or self.request.candidate_digest != self.candidate.digest
            or self.request.policy_digest != self.candidate.policy_digest
            or self.execution_config_revision == self.candidate.proof.original_config_revision
        ):
            raise ValueError("Checkpoint mode receipt differs from its reviewed admission")
        return self
