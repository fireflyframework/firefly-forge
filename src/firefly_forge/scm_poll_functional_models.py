# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit issue-bound assistant allowance, independent of polling V2 consent."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from firefly_forge.scm_authority_models import Digest, NativeID
from firefly_forge.scm_poll_models import Component, PollContract

FunctionalAction = Literal["assistant_start", "assistant_status", "assistant_turn"]


class SCMPollFunctionalGrantInputV1(PollContract):
    version: Literal[1]
    transport: Literal["github_rest_poll_v1"]
    request_id: NativeID
    expected_previous_digest: Digest | None
    expected_project_binding_digest: Digest
    expected_binding_request_id: NativeID
    expected_binding_digest: Digest
    component_ids: list[Component] = Field(min_length=1, max_length=50)
    issue_number: int = Field(ge=1)
    allowed_actions: list[FunctionalAction] = Field(min_length=1, max_length=3)
    max_model_turns: int = Field(ge=0, le=3)
    expires_at: str = Field(min_length=1, max_length=50)
    model_profile_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    expected_model_profile_revision: int = Field(ge=1)
    expected_model_profile_digest: Digest
    expected_routing_digest: Digest
    expected_functional_policy_digest: Digest
    active: bool
    acknowledge_separate_assistant_allowance: Literal[True]

    @field_validator("acknowledge_separate_assistant_allowance", mode="before")
    @classmethod
    def explicit_allowance(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Explicitly acknowledge the separate bounded assistant allowance")
        return value

    @field_validator("expires_at")
    @classmethod
    def aware_expiration(cls, value: str) -> str:
        if datetime.fromisoformat(value).utcoffset() is None:
            raise ValueError("Assistant consent expiration requires an explicit timezone")
        return value

    @model_validator(mode="after")
    def finite_scope(self) -> SCMPollFunctionalGrantInputV1:
        if len(set(self.component_ids)) != len(self.component_ids) or len(set(self.allowed_actions)) != len(
            self.allowed_actions
        ):
            raise ValueError("Functional components and actions must be unique")
        if ("assistant_turn" in self.allowed_actions) != (self.max_model_turns > 0):
            raise ValueError("Model-capable turns require their own finite allowance")
        return self


class SCMPollFunctionalGrantReceiptV1(PollContract):
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
    request: SCMPollFunctionalGrantInputV1
    request_digest: Digest
    digest: Digest
