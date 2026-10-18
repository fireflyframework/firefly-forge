# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit optional analytics binding; no query or execution authority."""

from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class AnalyticsConfigurationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_revision: int = Field(ge=0, le=32, strict=True)
    enabled: bool = Field(strict=True)
    endpoint: str = Field(max_length=2048)
    tenant_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    workspace_id: str
    dataset_id: str
    agent_token: SecretStr = Field(json_schema_extra={"writeOnly": True})

    @field_validator("endpoint")
    @classmethod
    def endpoint_origin(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
                and parsed.port in (None, 443)
                and parsed.path in ("", "/")
                and not parsed.query
                and not parsed.fragment
                and not any(c.isspace() for c in value)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Use a trusted HTTPS origin on port 443 without path or credentials")
        return value.rstrip("/")

    @field_validator("workspace_id", "dataset_id")
    @classmethod
    def remote_uuid(cls, value: str) -> str:
        return str(UUID(value))

    @field_validator("agent_token")
    @classmethod
    def scoped_token(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not 1 <= len(raw) <= 4096 or any(not 33 <= ord(c) <= 126 for c in raw):
            raise ValueError("Use a nonempty bounded single-line scoped agent token")
        return value
