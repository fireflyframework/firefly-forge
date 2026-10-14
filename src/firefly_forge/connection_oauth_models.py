# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded connection input; provider endpoints and scopes come from admitted policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class OAuthConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    project_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    expected_revision: int | None = Field(default=None, strict=True, ge=0, le=2_147_483_647)
    client_id: str = Field(min_length=1, max_length=200)
    client_secret: SecretStr | None = None
    public_client: bool | None = None
    authentication: Literal["client_secret_basic", "client_secret_post"] = "client_secret_post"

    @model_validator(mode="after")
    def credentials(self) -> OAuthConnectionInput:
        if any(ord(c) < 32 or ord(c) == 127 for c in self.client_id):
            raise ValueError("Client ID contains control characters")
        if self.client_secret is not None:
            secret = self.client_secret.get_secret_value()
            if not secret or len(secret.encode()) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in secret):
                raise ValueError("Supply a bounded nonempty client secret")
            if self.public_client:
                raise ValueError("A public client cannot retain a secret")
        if self.public_client and self.authentication == "client_secret_basic":
            raise ValueError("Basic authentication requires a confidential client")
        return self


class OAuthSupersedeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_revision: int = Field(strict=True, ge=1, le=2_147_483_647)
    unknown_effect_ids: list[str] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)
    acknowledge_unknown: Literal[True]
    registration: OAuthConnectionInput

    @field_validator("acknowledge_unknown", mode="before")
    @classmethod
    def explicit_acknowledgment(cls, value: object) -> bool:
        if value is not True:
            raise ValueError("Explicitly acknowledge that the old provider outcome remains unknown")
        return True

    @field_validator("unknown_effect_ids")
    @classmethod
    def exact_effect_ids(cls, values: list[str]) -> list[str]:
        import re

        if len(set(values)) != len(values) or any(not re.fullmatch(r"[a-f0-9]{32}", value) for value in values):
            raise ValueError("Select distinct exact unknown effect identifiers")
        return values

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Explain why this new admission is required")
        return value


class OAuthAllowedActions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    configure: bool
    authorize: bool
    refresh: bool
    disconnect: bool
    supersede: bool


class OAuthEffectSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    kind: Literal["code", "refresh"]
    state: Literal["executing", "unknown"]
    registration_revision: int = Field(ge=1, le=2_147_483_647)
    lease_expired: bool


class OAuthConnectionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    name: str
    revision: int
    provider: str
    client_id: str
    project_id: str | None
    status: str
    resource: str
    scopes: list[str]
    agent_ready: Literal[False]
    remote_revocation_supported: Literal[False]
    authentication: Literal["client_secret_basic", "client_secret_post"]
    public_client: bool
    secret_configured: bool
    callback_uri: str
    refresh_available: bool
    grant_expires_at: str | None
    effect_state: Literal["none", "executing", "unknown"]
    unresolved_effects: list[OAuthEffectSummary]
    archived_unknown_effects: list[OAuthEffectSummary]
    reconciliation_required: bool
    allowed_actions: OAuthAllowedActions
    family: str | None = None
    grant_revision: int | None = None
    grant_status: str | None = None
    observed_at: str | None = None
    policy_current: bool = True


class OAuthEffectReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    kind: Literal["code", "refresh"]
    state: Literal["executing", "unknown", "completed", "rejected"]
    registration_revision: int = Field(ge=1, le=2_147_483_647)
    lease_expired: bool
    observed_at: str | None
    current_context: bool


@dataclass(frozen=True)
class OAuthProvider:
    id: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    resource: str
    scopes: tuple[str, ...]
    scope_omission: Literal["reject", "requested"] = "reject"
    refresh_scope_omission: Literal["reject", "preserve"] = "reject"
    refresh_token_omission: Literal["reject", "preserve"] = "reject"
    prerequisite: str | None = None


@dataclass(frozen=True)
class OAuthPolicyBinding:
    provider: OAuthProvider
    revision: int
    digest: str


@dataclass(frozen=True)
class OAuthChallenge:
    authorization_url: str = field(repr=False)
    browser_nonce: str = field(repr=False)
    attempt_id: str


class OAuthDisposition(ValueError):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(kind)
