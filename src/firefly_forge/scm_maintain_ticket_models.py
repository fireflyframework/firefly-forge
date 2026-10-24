# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit finite communication consent binds one native maintain creation."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from firefly_forge.collaboration_feedback import MAX_FEEDBACK
from firefly_forge.journal import digest
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract


class MaintainTicketInput(StrictContract):
    version: Literal[1]
    request_id: NativeID
    run_id: NativeID
    creation_request_id: NativeID
    expected_creation_digest: Digest
    observed_receipt_id: NativeID
    expected_reference_digest: Digest
    expected_previous_digest: Digest | None
    lifetime_seconds: int = Field(ge=60, le=3600)
    max_notifications: int = Field(ge=1, le=MAX_FEEDBACK)
    allowed_actions: list[Literal["status", "question", "answer"]] = Field(min_length=1, max_length=3)
    acknowledge_native_communication: Literal[True]

    @field_validator("acknowledge_native_communication", mode="before")
    @classmethod
    def explicit_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("Native communication consent must be an explicit boolean")
        return value

    @model_validator(mode="after")
    def exact_actions(self) -> MaintainTicketInput:
        if len(set(self.allowed_actions)) != len(self.allowed_actions) or set(self.allowed_actions) != {
            "status",
            "question",
            "answer",
        }:
            raise ValueError("This native binding requires explicit status, question and answer consent")
        return self


class MaintainTicketMarker(StrictContract):
    version: Literal[1]
    kind: Literal["native_maintain_ticket_v1"]
    project_id: NativeID
    request_id: NativeID
    run_id: NativeID
    binding_digest: Digest


class MaintainTicketReceipt(StrictContract):
    version: Literal[1]
    event_id: NativeID
    project_id: NativeID
    repository_id: NativeID
    run_id: NativeID
    binding_key: NativeID
    revision: int = Field(ge=1, le=100)
    previous_digest: Digest | None
    request: MaintainTicketInput
    request_digest: Digest
    admin_snapshot: dict[str, Any]
    creation_digest: Digest
    parent_reference: dict[str, Any]
    parent_observation: dict[str, Any]
    observation: dict[str, Any]
    polling_scope: dict[str, Any]
    owner: dict[str, Any]
    outbox_id: NativeID
    prepared_at_ms: int = Field(ge=1)
    expires_at_ms: int = Field(ge=1)
    policy_digest: Digest
    digest: Digest

    @model_validator(mode="after")
    def exact_binding(self) -> MaintainTicketReceipt:
        value = self.model_dump(mode="json")
        if (
            self.request_digest != request_digest(self.request)
            or self.run_id != self.request.run_id
            or self.binding_key != self.request.run_id
            or self.creation_digest != self.request.expected_creation_digest
            or self.previous_digest != self.request.expected_previous_digest
            or self.expires_at_ms != self.prepared_at_ms + self.request.lifetime_seconds * 1000
            or digest({k: v for k, v in value.items() if k != "digest"}) != self.digest
        ):
            raise ValueError("The immutable maintain ticket receipt changed")
        return self


def request_digest(request: MaintainTicketInput) -> str:
    return digest(request.model_dump(mode="json"))


def confirmed_remote(remote: dict[str, Any], ticket_url: str) -> dict[str, str]:
    value = remote.get("id")
    if type(value) not in {int, str} or re.fullmatch(r"[1-9][0-9]{0,39}", str(value)) is None:
        raise ValueError("The maintain notification has no exact remote comment identity")
    url = ticket_url + "#issuecomment-" + str(value)
    if remote.get("url") != url:
        raise ValueError("The maintain notification belongs to another issue or comment")
    return {"id": str(value), "url": url}
