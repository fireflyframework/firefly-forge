# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native consent restores only an exactly retained and still-current selection."""

from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from firefly_forge.checkpoint_mode_models import CheckpointModeActorV1
from firefly_forge.journal import digest
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract


class OriginalSelectionReadmissionInput(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    candidate_digest: Digest
    policy_digest: Digest
    operator_observation_digest: Digest
    observation_provenance: Literal["native_operator_pre_dispatch_observation"]
    acknowledge_known_local_readiness_failure: Literal[True]
    acknowledge_no_external_model_dispatch: Literal[True]
    acknowledge_original_selection_only: Literal[True]
    acknowledge_original_mode_and_claim_preserved: Literal[True]
    acknowledge_spent_history_and_budgets_preserved: Literal[True]
    acknowledge_same_native_continuation: Literal[True]
    acknowledge_existing_unknown_command_risk: Literal[True]

    @field_validator(
        "acknowledge_known_local_readiness_failure",
        "acknowledge_no_external_model_dispatch",
        "acknowledge_original_selection_only",
        "acknowledge_original_mode_and_claim_preserved",
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


class OriginalSelectionProofV1(StrictContract):
    version: Literal[1]
    run_id: NativeID
    task_key: NativeID
    attempt: int = Field(ge=1, strict=True)
    run_digest: Digest
    task_digest: Digest
    journal_digest: Digest
    handoff_digest: Digest
    mode_receipt_digest: Digest
    mode_claim_digest: Digest
    startup_recovery_digest: Digest | None
    selection_digest: Digest
    profile_digest: Digest
    routing_digest: Digest
    original_settings_digest: Digest
    ineffective_settings_digest: Digest
    current_settings_digest: Digest
    frozen_budget_digest: Digest
    digest: Digest

    @model_validator(mode="after")
    def exact_digest(self) -> "OriginalSelectionProofV1":
        if digest(self.model_dump(exclude={"digest"})) != self.digest:
            raise ValueError("Original selection proof digest changed")
        return self


class OriginalSelectionCandidateV1(StrictContract):
    version: Literal[1]
    run_id: NativeID
    actor: CheckpointModeActorV1
    policy_digest: Digest
    checkpoint_mode_policy_digest: Digest
    acknowledgement_policy_digest: Digest
    original_mode_policy_digest: Digest
    original_acknowledgement_policy_digest: Digest
    original_scope_digest: Digest
    source_digest: Digest
    historical_custody_digest: Digest
    verification_custody_digest: Digest
    execution_config_revision: NativeID
    profile_id: str = Field(min_length=1)
    proof: OriginalSelectionProofV1
    digest: Digest

    @model_validator(mode="after")
    def exact_digest(self) -> "OriginalSelectionCandidateV1":
        if self.run_id != self.proof.run_id or digest(self.model_dump(exclude={"digest"})) != self.digest:
            raise ValueError("Original selection candidate digest changed")
        return self


class OriginalSelectionReceiptV1(StrictContract):
    version: Literal[1]
    run_id: NativeID
    request_id: NativeID
    candidate: OriginalSelectionCandidateV1
    request: OriginalSelectionReadmissionInput
    readmission_intent_digest: Digest

    @model_validator(mode="after")
    def exact_request(self) -> "OriginalSelectionReceiptV1":
        if (
            self.run_id != self.candidate.run_id
            or self.request.candidate_digest != self.candidate.digest
            or self.request.policy_digest != self.candidate.policy_digest
        ):
            raise ValueError("Original selection receipt differs from its reviewed admission")
        return self
