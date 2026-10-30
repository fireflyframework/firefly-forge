# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit consent to known worker output, never a new execution grant."""

from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from firefly_forge.journal import digest
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract


class WorkerCompletionInputV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    candidate_digest: Digest
    policy_digest: Digest
    expected_run_digest: Digest
    journal_digest: Digest
    proof_digest: Digest
    feedback_record_digest: Digest
    acknowledge_known_worker_output: Literal[True]
    acknowledge_no_execution_grant: Literal[True]

    @field_validator("acknowledge_known_worker_output", "acknowledge_no_execution_grant", mode="before")
    @classmethod
    def exact_acknowledgment(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError("Acknowledge known output without a new execution grant")
        return value


class CompletionCustodyV1(StrictContract):
    request_id: str = Field(pattern=r"^fbr1_[a-f0-9]{32}$")
    intent_digest: Digest
    checkpoint_record_digest: Digest
    response_record_digest: Digest
    consume_record_digest: Digest


class ConsumedTerminalInputV2(StrictContract):
    version: Literal[2]
    candidate_digest: Digest
    policy_digest: Digest
    expected_run_digest: Digest
    journal_digest: Digest
    proof_digest: Digest
    acknowledge_known_worker_output: Literal[True]
    acknowledge_no_execution_grant: Literal[True]
    acknowledge_local_postprocessing: Literal[True]

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> Any:
        if type(value) is not int or value != 2:
            raise ValueError("Use the exact consumed terminal version")
        return value

    @field_validator(
        "acknowledge_known_worker_output",
        "acknowledge_no_execution_grant",
        "acknowledge_local_postprocessing",
        mode="before",
    )
    @classmethod
    def exact_acknowledgment(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError("Acknowledge the exact consumed result and local postprocessing without a new grant")
        return value


class ConsumedTerminalProofV2(StrictContract):
    version: Literal[2]
    run_id: NativeID
    task_id: Literal["implement"]
    task_attempt: int = Field(ge=1, strict=True)
    task_key: NativeID
    completion_count: int = Field(ge=1, strict=True)
    completions: list[CompletionCustodyV1]
    terminal_report_sha256: Digest
    final_response_sha256: Digest
    recorded_runtime_contract: Digest
    current_runtime_contract: Digest
    current_profile_digest: Digest
    source_digest: Digest
    parent_final_digest: Digest
    handoff_custody_digest: Digest
    usage_before_digest: Digest
    usage_after_digest: Digest
    verification: Literal["consumed_terminal_output_known_only"]
    digest: Digest

    @model_validator(mode="after")
    def exact_proof(self) -> "ConsumedTerminalProofV2":
        if (
            self.completion_count != len(self.completions)
            or len({item.request_id for item in self.completions}) != self.completion_count
            or digest(self.model_dump(exclude={"digest"})) != self.digest
        ):
            raise ValueError("Consumed terminal proof differs from its exact immutable custody")
        return self


class ConsumedTerminalCandidateV2(StrictContract):
    version: Literal[2]
    run_id: NativeID
    actor_digest: Digest
    policy_digest: Digest
    run_digest: Digest
    journal_digest: Digest
    proof: ConsumedTerminalProofV2
    digest: Digest

    @model_validator(mode="after")
    def exact_digest(self) -> "ConsumedTerminalCandidateV2":
        if self.proof.run_id != self.run_id or digest(self.model_dump(exclude={"digest"})) != self.digest:
            raise ValueError("Consumed terminal candidate digest differs")
        return self


class CompletionHoldV1(StrictContract):
    version: Literal[1]
    reason: Literal["graph_checkpoint_unavailable"]
    request_id: str = Field(pattern=r"^fbr1_[a-f0-9]{32}$")
    intent_digest: Digest


class WorkerCompletionProofV1(StrictContract):
    version: Literal[1]
    run_id: NativeID
    task_id: Literal["implement"]
    task_attempt: int = Field(ge=1)
    task_key: str = Field(min_length=1, max_length=64)
    runtime_contract: Digest
    completion_count: int = Field(ge=1)
    completions: list[CompletionCustodyV1]
    feedback_record_digest: Digest
    feedback_sha256: Digest
    final_response_sha256: Digest
    observed_hold_digest: Digest
    verification: Literal["worker_output_known_only"]
    digest: Digest

    @model_validator(mode="after")
    def exact_proof(self) -> "WorkerCompletionProofV1":
        if (
            self.completion_count != len(self.completions)
            or len({item.request_id for item in self.completions}) != self.completion_count
            or digest(self.model_dump(exclude={"digest"})) != self.digest
        ):
            raise ValueError("Known worker completion proof differs from complete custody")
        return self


class WorkerCompletionCandidateV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    run_id: NativeID
    actor_digest: Digest
    policy_digest: Digest
    run_digest: Digest
    journal_digest: Digest
    proof: WorkerCompletionProofV1
    digest: Digest

    @model_validator(mode="after")
    def exact_digest(self) -> "WorkerCompletionCandidateV1":
        if digest(self.model_dump(exclude={"digest"})) != self.digest:
            raise ValueError("Worker completion candidate digest differs")
        return self


class WorkerCompletionReceiptV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    run_id: NativeID
    request_id: NativeID
    actor_digest: Digest
    candidate_digest: Digest
    policy_digest: Digest
    run_digest: Digest
    journal_digest: Digest
    request: WorkerCompletionInputV1
    proof: WorkerCompletionProofV1
    observed_hold: CompletionHoldV1

    @model_validator(mode="after")
    def exact_receipt(self) -> "WorkerCompletionReceiptV1":
        proof = self.proof.model_dump(mode="json")
        if (
            self.request.candidate_digest != self.candidate_digest
            or self.request.policy_digest != self.policy_digest
            or self.request.expected_run_digest != self.run_digest
            or self.request.journal_digest != self.journal_digest
            or self.request.proof_digest != proof.get("digest")
            or self.request.feedback_record_digest != proof.get("feedback_record_digest")
            or proof.get("run_id") != self.run_id
            or digest(self.observed_hold.model_dump(mode="json")) != proof.get("observed_hold_digest")
            or digest({k: v for k, v in proof.items() if k != "digest"}) != proof.get("digest")
        ):
            raise ValueError("Known worker receipt differs from its reviewed proof")
        return self


class ConsumedTerminalReceiptV2(StrictContract):
    version: Literal[2]
    kind: Literal["consumed_terminal_recovery"]
    run_id: NativeID
    request_id: NativeID
    record_digest: Digest
    candidate: ConsumedTerminalCandidateV2
    request: ConsumedTerminalInputV2
    state: Literal["queued", "claimed", "accepted"]
    run_status: str = Field(min_length=1, max_length=64)
    same_task_attempt: int = Field(ge=1, strict=True)
    no_execution_grant: Literal[True]

    @field_validator("no_execution_grant", mode="before")
    @classmethod
    def exact_no_grant(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError("Consumed terminal acceptance does not grant execution")
        return value

    @model_validator(mode="after")
    def exact_receipt(self) -> "ConsumedTerminalReceiptV2":
        if (
            self.run_id != self.candidate.run_id
            or self.same_task_attempt != self.candidate.proof.task_attempt
            or any(
                getattr(self.request, key) != value
                for key, value in {
                    "candidate_digest": self.candidate.digest,
                    "policy_digest": self.candidate.policy_digest,
                    "expected_run_digest": self.candidate.run_digest,
                    "journal_digest": self.candidate.journal_digest,
                    "proof_digest": self.candidate.proof.digest,
                }.items()
            )
        ):
            raise ValueError("Consumed terminal receipt differs from its exact reviewed request")
        return self
