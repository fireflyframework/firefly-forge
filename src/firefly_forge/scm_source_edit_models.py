# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Internal verified handoff for one administrator-sealed SCM source activation."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from firefly_forge.identity import Principal
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract
from firefly_forge.source_edit_models import SourceEditContinuationInput


@dataclass(frozen=True)
class SealedSourceEditAdmission:
    run_id: str
    native_actor: dict[str, Any]
    caller: Principal
    request: SourceEditContinuationInput
    evidence: dict[str, Any]
    verify: Callable[[], Awaitable[None]]


class SCMSourceEditCommandV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    action: Literal["source_edit"]
    run_id: NativeID
    expected_grant_digest: Digest


class SCMSourceEditGrantInputV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    transport: Literal["github_rest_poll_v1"]
    request_id: NativeID
    expected_previous_digest: Digest | None
    expected_project_binding_digest: Digest
    expected_binding_request_id: NativeID
    expected_binding_digest: Digest
    component_ids: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]] = Field(min_length=1, max_length=50)
    issue_number: int = Field(ge=1)
    run_id: NativeID
    activation: SourceEditContinuationInput
    expected_source_profile_digest: Digest
    expires_at: str = Field(min_length=1, max_length=50)
    active: bool
    acknowledge_same_run_source_only: Literal[True]

    @field_validator("acknowledge_same_run_source_only", mode="before")
    @classmethod
    def explicit_consent(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError("Explicitly consent to the exact existing source-only task")
        return value

    @model_validator(mode="after")
    def finite_scope(self) -> "SCMSourceEditGrantInputV1":
        try:
            aware = datetime.fromisoformat(self.expires_at).utcoffset() is not None
        except ValueError:
            raise ValueError("Select an absolute source consent expiration") from None
        if len(set(self.component_ids)) != len(self.component_ids) or not aware:
            raise ValueError("Select unique components and an absolute consent expiration")
        return self


class SCMSourceEditGrantReceiptV1(StrictContract):
    version: Literal[1]
    event_id: NativeID
    project_id: NativeID
    repository_id: NativeID
    binding_key: Digest
    native_user_id: NativeID
    revision: int = Field(ge=1, le=50)
    previous_digest: Digest | None
    prepared_at: str = Field(min_length=1, max_length=50)
    administering_user_id: NativeID
    admin_snapshot: dict[str, Any]
    request: SCMSourceEditGrantInputV1
    request_digest: Digest
    digest: Digest
