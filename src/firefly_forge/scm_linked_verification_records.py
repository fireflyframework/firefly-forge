# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Typed retained review, human confirmation and one-shot SCM verification intent."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from firefly_forge.execution_requests import digest as request_digest
from firefly_forge.journal import digest
from firefly_forge.linked_verification_models import (
    LinkedVerificationCandidateV1,
    LinkedVerificationInputV1,
    LinkedVerificationReceiptV1,
)
from firefly_forge.models import Command, NewWorkCommand
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract
from firefly_forge.scm_conversation_models import ConversationReadScopeV1
from firefly_forge.scm_linked_verification_models import SCMLinkedVerificationScopeV1


class NativeHostAdministratorV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    native_user_id: NativeID
    issuer: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    role_audience: str = Field(min_length=1)
    roles_digest: Digest
    issued_at: float
    expires_at: float


class SCMVerificationBaseV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    proposal_id: NativeID
    run_id: NativeID
    component_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    source_digest: Digest
    issue_kind: Literal["issue", "pull_request"]
    issue_number: int = Field(ge=1)
    external_actor_id: str = Field(pattern=r"^[1-9][0-9]{0,39}$")
    read_scope: ConversationReadScopeV1
    host_administrator: NativeHostAdministratorV1
    digest: Digest

    @model_validator(mode="after")
    def exact_base(self) -> SCMVerificationBaseV1:
        if (
            self.proposal_id != self.read_scope.verified_receipt_id
            or self.read_scope.transport != "github_rest_poll_v1"
            or self.component_id not in self.read_scope.component_ids
            or self.host_administrator.native_user_id != self.read_scope.native_user_id
            or digest(self.model_dump(mode="json", exclude={"digest"})) != self.digest
        ):
            raise ValueError("SCM verification proposal differs from its verified native owner")
        return self

    def actor_scope(self) -> SCMLinkedVerificationScopeV1:
        return SCMLinkedVerificationScopeV1(
            version=1,
            transport="github_rest_poll_linked_verification_v1",
            proposal_id=self.proposal_id,
            proposal_digest=self.digest,
            project_id=self.read_scope.project_id,
            repository_id=self.read_scope.repository_id,
            component_id=self.component_id,
            run_id=self.run_id,
            native_user_id=self.read_scope.native_user_id,
            binding_key=self.read_scope.binding_key,
            binding_digest=self.read_scope.binding_digest,
            binding_request_id=self.read_scope.binding_request_id,
        )


class SCMVerificationReviewV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    base_digest: Digest
    candidate: LinkedVerificationCandidateV1
    language: Literal["en", "es"]
    question: str = Field(min_length=1, max_length=4000)
    digest: Digest

    @model_validator(mode="after")
    def exact_review(self) -> SCMVerificationReviewV1:
        if digest(self.model_dump(mode="json", exclude={"digest"})) != self.digest:
            raise ValueError("The native verification review changed")
        return self


class SCMVerificationPublicationV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    receipt_id: NativeID
    comment_id: str = Field(pattern=r"^[1-9][0-9]{0,39}$")
    body_sha256: Digest


class SCMVerificationConfirmationV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    review_digest: Digest
    receipt_id: NativeID
    source_digest: Digest
    human_message: str = Field(min_length=1, max_length=12000)
    human_sha256: Digest
    affirmative_span: str = Field(min_length=1, max_length=12000)
    interpretation_digest: Digest
    digest: Digest

    @model_validator(mode="after")
    def exact_confirmation(self) -> SCMVerificationConfirmationV1:
        if (
            not self.affirmative_span.strip()
            or self.affirmative_span not in self.human_message
            or hashlib.sha256(self.human_message.encode()).hexdigest() != self.human_sha256
            or digest(self.model_dump(mode="json", exclude={"digest"})) != self.digest
        ):
            raise ValueError("The verification confirmation lost its exact human source")
        return self


class SCMVerificationGrantV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    review_digest: Digest
    publication: SCMVerificationPublicationV1
    confirmation: SCMVerificationConfirmationV1
    request: LinkedVerificationInputV1
    digest: Digest

    @model_validator(mode="after")
    def exact_grant(self) -> SCMVerificationGrantV1:
        if (
            self.review_digest != self.confirmation.review_digest
            or digest(self.model_dump(mode="json", exclude={"digest"})) != self.digest
        ):
            raise ValueError("The native verification grant changed")
        return self


class SCMVerificationIntentV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    request_id: NativeID
    request_digest: Digest
    state: Literal["admitting", "queued", "needs_reconciliation"]


class SCMVerificationRecordV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    base: SCMVerificationBaseV1
    review: SCMVerificationReviewV1 | None = None
    grant: SCMVerificationGrantV1 | None = None
    intent: SCMVerificationIntentV1 | None = None

    @model_validator(mode="after")
    def exact_chain(self) -> SCMVerificationRecordV1:
        actor_digest = digest(
            {
                "actor": {
                    "user_id": self.base.read_scope.native_user_id,
                    "authentication_method": "scm_mapping",
                    "global_admin": False,
                    "api_audience": None,
                    "scm_delegation": self.base.actor_scope().model_dump(mode="json"),
                },
                "identity": {
                    "issuer": self.base.host_administrator.issuer,
                    "subject": self.base.host_administrator.subject,
                },
            }
        )
        if self.review is not None and (
            self.review.base_digest != self.base.digest
            or self.review.candidate.run_id != self.base.run_id
            or self.review.candidate.actor_digest != actor_digest
        ):
            raise ValueError("Verification review belongs to another proposal")
        if self.grant is not None and (
            self.review is None
            or self.grant.review_digest != self.review.digest
            or self.grant.request.candidate_digest != self.review.candidate.digest
            or self.grant.request.request_id != self.base.proposal_id
            or self.grant.publication.receipt_id != self.base.proposal_id
        ):
            raise ValueError("Verification grant belongs to another exact review")
        if self.intent is not None and (
            self.grant is None
            or self.intent.request_id != self.grant.request.request_id
            or self.intent.request_digest != request_digest(self.grant.request.model_dump(mode="json"))
        ):
            raise ValueError("Verification admission intent belongs to another native request")
        return self


class SCMVerificationCommunicationV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1] = 1
    phase: Literal["review", "current", "unknown", "held"]
    candidate: LinkedVerificationCandidateV1 | None = None
    hold_reason: Literal["current_authority_or_review_changed", "operation_selection_ambiguous"] | None = None
    question: str | None = None
    receipt: LinkedVerificationReceiptV1 | None = None
    failure_command: Command | NewWorkCommand | None = None

    @model_validator(mode="after")
    def exact_operation(self) -> SCMVerificationCommunicationV1:
        if (self.phase == "held") != (self.hold_reason is not None) or (
            self.phase != "held" and self.candidate is None
        ):
            raise ValueError("Verification communication requires its exact current scope or explicit hold")
        if (self.phase == "review") != (self.question is not None) or (self.phase == "current") != (
            self.receipt is not None
        ):
            raise ValueError("Verification communication must identify its actual phase")
        if self.receipt is not None and self.receipt.candidate != self.candidate:
            raise ValueError("Verification communication belongs to another exact operation")
        if self.failure_command is not None and (
            self.receipt is None
            or self.receipt.last_failure is None
            or digest(self.failure_command.model_dump(mode="json")) != self.receipt.last_failure.command_digest
        ):
            raise ValueError("Failure command differs from its recorded native command result")
        return self
