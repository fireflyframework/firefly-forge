# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit command-custody consent; original effects and spent slots stay intact."""

from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from firefly_forge.journal import digest
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract


class CommandCustodyContract(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class VerificationFollowupV1(CommandCustodyContract):
    adoption_request_id: NativeID
    pending_request_digest: Digest
    source_digest: Digest
    commands_digest: Digest
    policy_digest: Digest
    provider_digest: Digest
    sandbox_digest: Digest
    scope_digest: Digest
    agent: Literal["agentic", "bridge", "codex", "claude"]
    parent_attempt: int = Field(ge=1)
    continuation_task_attempt: int = Field(ge=1)
    continuation_scope_digest: Digest
    continuation: int = Field(ge=1)
    ordinals: list[int] = Field(min_length=1)

    @field_validator("ordinals")
    @classmethod
    def exact_ordinals(cls, value: list[int]) -> list[int]:
        if any(n < 1 for n in value) or value != list(range(value[0], value[0] + len(value))):
            raise ValueError("Follow-up verification requires exact consecutive new ordinals")
        return value

    @model_validator(mode="after")
    def exact_continuation_attempt(self) -> "VerificationFollowupV1":
        if self.continuation_task_attempt != self.parent_attempt + 1:
            raise ValueError("Follow-up custody binds exactly the native verification continuation task")
        return self


class UnknownCommandAcknowledgementV1(CommandCustodyContract):
    version: Literal[1]
    candidate_digest: Digest
    policy_digest: Digest
    expected_run_digest: Digest
    journal_digest: Digest
    execution_digest: Digest
    classification: Literal["start_unknown", "closed_before_start"]
    followup: VerificationFollowupV1
    acknowledge_result_unknown: Literal[True] | None = None
    acknowledge_effects_may_have_occurred: Literal[True] | None = None
    acknowledge_no_start_intent: Literal[True] | None = None
    acknowledge_no_original_replay: Literal[True]
    acknowledge_bound_verification_and_continuation: Literal[True]

    @field_validator(
        "acknowledge_result_unknown",
        "acknowledge_effects_may_have_occurred",
        "acknowledge_no_start_intent",
        "acknowledge_no_original_replay",
        "acknowledge_bound_verification_and_continuation",
        mode="before",
    )
    @classmethod
    def exact_acknowledgement(cls, value: Any) -> Any:
        if value is not None and value is not True:
            raise ValueError("Explicitly acknowledge unknown effects without replay or a new execution budget")
        return value

    @model_validator(mode="after")
    def exact_classification(self) -> "UnknownCommandAcknowledgementV1":
        if self.classification == "start_unknown":
            if (
                self.acknowledge_result_unknown is not True
                or self.acknowledge_effects_may_have_occurred is not True
                or self.acknowledge_no_start_intent is not None
            ):
                raise ValueError("Acknowledge the unknown original result and possible effects")
        elif (
            self.acknowledge_no_start_intent is not True
            or self.acknowledge_result_unknown is not None
            or self.acknowledge_effects_may_have_occurred is not None
        ):
            raise ValueError("Acknowledge the closed execution's absence of a native start intent")
        return self


class UnknownCommandCandidateV1(CommandCustodyContract):
    version: Literal[1]
    run_id: NativeID
    execution_id: NativeID
    actor_digest: Digest
    policy_digest: Digest
    run_digest: Digest
    task_digest: Digest
    journal_digest: Digest
    execution_digest: Digest
    classification: Literal["start_unknown", "closed_before_start"]
    start_intent_digest: Digest | None
    ordinal: int = Field(ge=1)
    followup: VerificationFollowupV1
    digest: Digest

    @model_validator(mode="after")
    def exact_digest(self) -> "UnknownCommandCandidateV1":
        if (self.classification == "start_unknown") != (self.start_intent_digest is not None):
            raise ValueError("The command classification differs from its original start custody")
        if digest(self.model_dump(exclude={"digest"})) != self.digest:
            raise ValueError("Unknown-command candidate digest differs")
        return self


class NativeAcknowledgingActorV1(CommandCustodyContract):
    user_id: NativeID
    authentication_method: Literal["oidc_session", "oidc_bearer", "local_bootstrap"]
    global_admin: bool
    api_audience: str | None


class UnknownCommandReceiptV1(CommandCustodyContract):
    version: Literal[1]
    disposition: Literal["unknown_abandoned_for_verification", "closed_before_start_retired_for_verification"]
    run_id: NativeID
    execution_id: NativeID
    request_id: NativeID
    actor: NativeAcknowledgingActorV1
    candidate: UnknownCommandCandidateV1
    request: UnknownCommandAcknowledgementV1
    followup: VerificationFollowupV1

    @model_validator(mode="after")
    def exact_binding(self) -> "UnknownCommandReceiptV1":
        c, r = self.candidate, self.request
        if (
            c.run_id != self.run_id
            or c.execution_id != self.execution_id
            or digest(self.actor.model_dump()) != c.actor_digest
            or r.candidate_digest != c.digest
            or r.policy_digest != c.policy_digest
            or r.expected_run_digest != c.run_digest
            or r.journal_digest != c.journal_digest
            or r.execution_digest != c.execution_digest
            or r.classification != c.classification
            or self.disposition
            != {
                "start_unknown": "unknown_abandoned_for_verification",
                "closed_before_start": "closed_before_start_retired_for_verification",
            }[c.classification]
            or r.followup != c.followup
            or self.followup != c.followup
        ):
            raise ValueError("Unknown-command acknowledgement differs from its reviewed candidate")
        return self
