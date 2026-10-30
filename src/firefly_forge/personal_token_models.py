# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit project observation grants and owner-chosen credential expiry."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FactoryId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


def _read_permissions() -> list[Literal["read"]]:
    return ["read"]


class PersonalTokenInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: FactoryId
    name: str = Field(min_length=1, max_length=160)
    project_ids: list[FactoryId] = Field(min_length=1)
    permissions: list[Literal["read"]] = Field(default_factory=_read_permissions, min_length=1, max_length=1)
    expires_at: datetime

    @field_validator("project_ids")
    @classmethod
    def distinct_projects(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Choose each project once")
        return sorted(value)

    @field_validator("name")
    @classmethod
    def public_name(cls, value: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Credential names must be plain text")
        return value

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Choose an expiry with an explicit timezone")
        return value


class PersonalTokenRotationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: FactoryId
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        return PersonalTokenInput.aware_expiry(value)
