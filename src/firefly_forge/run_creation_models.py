# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit creation identities without changing the legacy RunInput wire contract."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
