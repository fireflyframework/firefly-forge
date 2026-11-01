# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native SCM verification identity and separately sealed one-operation consent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import ConfigDict, Field, model_validator

from firefly_forge.journal import digest
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract

if TYPE_CHECKING:
    from firefly_forge.identity import Principal
    from firefly_forge.linked_verification_models import LinkedVerificationCandidateV1, LinkedVerificationInputV1

Data = dict[str, Any]


class SCMLinkedVerificationScopeV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    transport: Literal["github_rest_poll_linked_verification_v1"]
    proposal_id: NativeID
    proposal_digest: Digest
    project_id: NativeID
    repository_id: NativeID
    component_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    run_id: NativeID
    native_user_id: NativeID
    binding_key: Digest
    binding_digest: Digest
    binding_request_id: NativeID


class SCMLinkedVerificationAdmissionV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    scope: SCMLinkedVerificationScopeV1
    candidate_digest: Digest
    request_id: NativeID
    request_digest: Digest
    review_digest: Digest
    publication_digest: Digest
    confirmation_digest: Digest
    host_administrator_digest: Digest
    grant_digest: Digest
    digest: Digest

    @model_validator(mode="after")
    def exact_admission(self) -> SCMLinkedVerificationAdmissionV1:
        if self.request_id != self.scope.proposal_id or digest(self.model_dump(exclude={"digest"})) != self.digest:
            raise ValueError("SCM verification consent differs from its one exact operation")
        return self


class PreparedSCMLinkedVerificationAdmission(Protocol):
    @property
    def seal(self) -> SCMLinkedVerificationAdmissionV1: ...

    async def current(self) -> None: ...

    def fence(self) -> None: ...


class SCMLinkedVerificationAuthority(Protocol):
    async def review(self, actor: Principal, run_id: str) -> None: ...

    async def admit(
        self,
        actor: Principal,
        run_id: str,
        candidate: LinkedVerificationCandidateV1,
        request: LinkedVerificationInputV1,
    ) -> PreparedSCMLinkedVerificationAdmission: ...

    async def worker(
        self, saved_actor: Data, identity: Data, operation_data: Data
    ) -> PreparedSCMLinkedVerificationAdmission: ...
