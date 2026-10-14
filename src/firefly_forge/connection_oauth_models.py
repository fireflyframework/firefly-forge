# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded connection input; provider endpoints and scopes come from admitted policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class OAuthConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    project_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
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
class OAuthChallenge:
    authorization_url: str = field(repr=False)
    browser_nonce: str = field(repr=False)
    attempt_id: str


class OAuthDisposition(ValueError):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(kind)
