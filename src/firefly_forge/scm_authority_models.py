# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Versioned, scoped delegation contracts; provider identities are not native logins."""

from __future__ import annotations

import re
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NativeID = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Provider = Literal["github", "gitlab", "bitbucket"]
DelegatedAction = Literal["respond", "status", "cancel"]


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @field_validator("version", mode="before", check_fields=False)
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("The version discriminator must be an exact integer")
        return value


def exact_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.netloc != parsed.netloc.lower()
        or parsed.hostname.endswith(".")
        or any(ord(character) <= 32 for character in value)
    ):
        raise ValueError("Use the exact configured HTTPS origin")
    _ = parsed.port
    return value


def canonical_provider_id(provider: Provider, value: str) -> str:
    if provider == "bitbucket":
        canonical = str(UUID(value))
        if value.lower() not in {canonical, "{" + canonical + "}"}:
            raise ValueError("Use a provider UUID, not an account label")
        return canonical
    if not re.fullmatch(r"[1-9][0-9]{0,39}", value):
        raise ValueError("Use canonical positive provider account and repository IDs")
    return value


class ProviderIdentity(StrictContract):
    provider: Provider
    api_origin: str = Field(max_length=500)
    installation_id: str | None
    remote_repository_id: str = Field(min_length=1, max_length=200)
    external_actor_id: str = Field(min_length=1, max_length=200)

    @field_validator("api_origin")
    @classmethod
    def exact_api_origin(cls, value: str) -> str:
        return exact_origin(value)

    @model_validator(mode="after")
    def provider_identity(self) -> ProviderIdentity:
        for name in ("remote_repository_id", "external_actor_id"):
            object.__setattr__(self, name, canonical_provider_id(self.provider, getattr(self, name)))
        if self.provider == "github":
            if self.installation_id is None or not re.fullmatch(r"[1-9][0-9]{0,39}", self.installation_id):
                raise ValueError("A GitHub App installation is required")
        elif self.installation_id is not None:
            raise ValueError("This provider has no GitHub App installation")
        return self


class VerifiedSCMReferenceV1(ProviderIdentity):
    """Server-derived verification cut, never accepted from an administration body."""

    version: Literal[1]
    web_origin: str = Field(max_length=500)
    connection_revision: NativeID
    connection_scope_digest: Digest
    webhook_key_revision: NativeID
    verification_method: Literal["raw_body_hmac_sha256", "gitlab_signing_hmac_sha256"]
    payload_digest: Digest

    @field_validator("web_origin")
    @classmethod
    def exact_origin(cls, value: str) -> str:
        return exact_origin(value)

    @model_validator(mode="after")
    def signed_provider(self) -> VerifiedSCMReferenceV1:
        expected = "gitlab_signing_hmac_sha256" if self.provider == "gitlab" else "raw_body_hmac_sha256"
        if self.verification_method != expected:
            raise ValueError("Delegation requires the configured provider payload signature")
        return self


class SCMActorBindingInputV1(StrictContract):
    version: Literal[1]
    request_id: NativeID
    expected_previous_digest: Digest | None
    expected_project_binding_digest: Digest
    verified_receipt_id: NativeID
    expected_verified_reference_digest: Digest
    native_user_id: NativeID
    component_ids: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]] = Field(min_length=1, max_length=50)
    allowed_actions: list[DelegatedAction] = Field(min_length=1, max_length=3)
    active: bool
    acknowledge_webhook_asserted_identity: Literal[True]

    @field_validator("acknowledge_webhook_asserted_identity", mode="before")
    @classmethod
    def exact_acknowledgment(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Explicitly acknowledge the configured webhook sender trust boundary")
        return value

    @model_validator(mode="after")
    def explicit_scope(self) -> SCMActorBindingInputV1:
        if not self.acknowledge_webhook_asserted_identity:
            raise ValueError("Acknowledge the configured webhook sender trust boundary")
        if len(set(self.component_ids)) != len(self.component_ids) or len(set(self.allowed_actions)) != len(
            self.allowed_actions
        ):
            raise ValueError("Components and actions must be unique")
        return self


class SCMResponseProvenanceV1(ProviderIdentity):
    binding_request_id: NativeID
    binding_digest: Digest
    verified_receipt_id: NativeID
    connection_revision: NativeID


class SCMRespondingActorV2(StrictContract):
    version: Literal[2]
    user_id: NativeID
    authentication_method: Literal["scm_mapping"]
    provenance: SCMResponseProvenanceV1


class SCMActorBindingReceiptV1(StrictContract):
    version: Literal[1]
    event_id: NativeID
    project_id: NativeID
    repository_id: NativeID
    project_binding_digest: Digest
    binding_key: Digest
    revision: int = Field(ge=1, le=50)
    previous_digest: Digest | None
    prepared_at: str = Field(min_length=1, max_length=50)
    administering_user_id: NativeID
    request: SCMActorBindingInputV1
    request_digest: Digest
    verified_reference: VerifiedSCMReferenceV1
    digest: Digest


class SCMActorBindingSummaryV1(StrictContract):
    version: Literal[1]
    event_id: NativeID
    project_id: NativeID
    repository_id: NativeID
    project_binding_digest: Digest
    request_id: NativeID
    binding_key: Digest
    revision: int = Field(ge=1, le=50)
    digest: Digest
    native_user_id: NativeID
    component_ids: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]] = Field(min_length=1, max_length=50)
    allowed_actions: list[DelegatedAction] = Field(min_length=1, max_length=3)
    active: bool
    provider: Provider
    external_actor_id: str = Field(min_length=1, max_length=200)
    prepared_at: str = Field(min_length=1, max_length=50)
    summary_digest: Digest
