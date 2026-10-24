# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit creation identities without changing the legacy RunInput wire contract."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer, model_validator

from firefly_forge.checkpoint_test_models import ResolvedCheckpointV1
from firefly_forge.models import RunInput

ID = r"^[a-f0-9]{32}$"
HASH = r"^[a-f0-9]{64}$"
MAX_ENVELOPE_BYTES = 256 * 1024


class VersionOne(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1]

    @model_validator(mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> Any:
        if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
            raise ValueError("Run creation requires exact version 1")
        return value


class RunCreationInput(VersionOne):
    input: RunInput


class CreationActor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    user_id: str = Field(pattern=ID)
    authentication_method: Literal["oidc_session", "oidc_bearer", "local_bootstrap"]


class RunCreationReceipt(VersionOne):
    request_id: str = Field(pattern=ID)
    original_actor: CreationActor
    request_digest: str = Field(pattern=HASH)
    input: dict[str, Any]
    repository_id: str = Field(pattern=ID)
    project_id: str | None = Field(pattern=ID)
    component_id: str | None = Field(default=None, max_length=64)
    run_id: str = Field(pattern=ID)
    admitted_at: str = Field(min_length=1, max_length=80)
    admission_digest: str = Field(pattern=HASH)
    digest: str = Field(pattern=HASH)
    source_checkpoint: ResolvedCheckpointV1 | None = None

    @model_serializer(mode="wrap")
    def retained_creation(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        value: dict[str, Any] = handler(self)
        if value.get("source_checkpoint") is None:
            value.pop("source_checkpoint", None)
        return value

    @model_validator(mode="after")
    def source_provenance(self) -> RunCreationReceipt:
        from firefly_forge.git_deliveries import delivery_digest

        requested = self.input.get("source_checkpoint")
        if self.source_checkpoint is None:
            if requested is not None:
                raise ValueError("Checkpoint creation receipt has no resolved source")
            return self
        source = self.source_checkpoint.model_dump(mode="json")
        if (
            requested != source["reference"]
            or self.input.get("operation") != source.get("operation", "test")
            or self.input.get("publish", False) is not False
            or any(getattr(self, key) != source[key] for key in ("repository_id", "project_id", "component_id"))
            or source["revision"] != source["reference"]["expected_revision"]
            or source["digest"] != delivery_digest({key: value for key, value in source.items() if key != "digest"})
            or self.run_id == source["reference"]["run_id"]
        ):
            raise ValueError("Checkpoint creation source differs from its original request and scope")
        return self


MAX_CONTROL_BYTES = 4096
MAX_PENDING_RESERVATIONS = 50


class RunCreationCancelInput(VersionOne):
    request_digest: str = Field(pattern=HASH)
    repository_id: str = Field(pattern=ID)
    project_id: str | None = Field(pattern=ID)
    component_id: str | None = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")

    @model_validator(mode="before")
    @classmethod
    def bounded_control(cls, value: Any) -> Any:
        import json

        if isinstance(value, dict) and len(json.dumps(value, ensure_ascii=False).encode()) > MAX_CONTROL_BYTES:
            raise ValueError("Run creation control exceeds 4 KiB")
        return value

    @model_validator(mode="after")
    def paired_scope(self) -> RunCreationCancelInput:
        if bool(self.project_id) != bool(self.component_id):
            raise ValueError("Choose both a product project and one of its components")
        return self


class RunCreationReservation(RunCreationCancelInput):
    request_id: str = Field(pattern=ID)
    original_actor: CreationActor
    reserved_at: str = Field(min_length=1, max_length=80)
    digest: str = Field(pattern=HASH)


class RunCreationCancellation(RunCreationCancelInput):
    request_id: str = Field(pattern=ID)
    original_actor: CreationActor
    cancelled_by: CreationActor
    cancelled_at: str = Field(min_length=1, max_length=80)
    digest: str = Field(pattern=HASH)

    @model_validator(mode="after")
    def same_owner(self) -> RunCreationCancellation:
        if self.original_actor.user_id != self.cancelled_by.user_id:
            raise ValueError("Cancellation belongs to another native user")
        return self


class RunCreationAdmittedDecision(VersionOne):
    outcome: Literal["admitted"]
    receipt: RunCreationReceipt


class RunCreationCancelledDecision(VersionOne):
    outcome: Literal["cancelled"]
    cancellation: RunCreationCancellation
