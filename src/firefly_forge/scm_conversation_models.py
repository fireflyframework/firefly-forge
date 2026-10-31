# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Direct status addresses carry read-only intent, never execution options."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Literal

from pydantic import (
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract, exact_origin

SCM_SOURCE_QUESTION_LIMIT = 40000


class AddressedStatusV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1] = 1
    action: Literal["status"] = "status"
    adapter: Literal["addressed_status_v1"] = "addressed_status_v1"
    intent: Literal["status", "clarify"]
    language: Literal["en", "es"]
    address: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,99}$")
    source_body_sha256: Digest


class ConversationReadScopeV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1] = 1
    action: Literal["status"] = "status"
    transport: Literal["github_rest_poll_v1", "signed_webhook_v1", "github_repository_webhook_v1"]
    project_id: NativeID
    repository_id: NativeID
    native_user_id: NativeID
    component_ids: list[str] = Field(min_length=1, max_length=50)
    binding_key: Digest
    binding_digest: Digest
    binding_request_id: NativeID
    verified_receipt_id: NativeID
    verified_reference_digest: Digest
    project_binding_digest: Digest
    observation_digest: Digest


class ConfiguredGitHubReadConnectionV1(StrictContract):
    """Server connection metadata, never an installation assertion in a webhook payload."""

    provenance: Literal["configured_github_application_v1"] = "configured_github_application_v1"
    provider: Literal["github"]
    web_origin: str = Field(max_length=500)
    api_origin: str = Field(max_length=500)
    connection_revision: NativeID
    connection_scope_digest: Digest
    installation_id: str = Field(pattern=r"^[1-9][0-9]{0,39}$")
    remote_repository_id: str = Field(pattern=r"^[1-9][0-9]{0,39}$")

    @field_validator("web_origin", "api_origin")
    @classmethod
    def origin(cls, value: str) -> str:
        return exact_origin(value)

    def scope(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"provenance"})


class GitHubRepositoryWebhookReferenceV1(StrictContract):
    """Repository-key HMAC plus a separately tagged configured read connection."""

    version: Literal[1] = 1
    transport: Literal["github_repository_webhook_v1"] = "github_repository_webhook_v1"
    provider: Literal["github"] = "github"
    connection: ConfiguredGitHubReadConnectionV1
    external_actor_id: str = Field(pattern=r"^[1-9][0-9]{0,39}$")
    webhook_key_revision: NativeID
    verification_method: Literal["repository_raw_body_hmac_sha256"] = "repository_raw_body_hmac_sha256"
    payload_digest: Digest
    source_digest: Digest

    def read_identity(self) -> dict[str, Any]:
        return {**self.connection.scope(), "external_actor_id": self.external_actor_id}


class SignedAddressedSourceV1(StrictContract):
    version: Literal[1] = 1
    payload_digest: Digest
    source_digest: Digest
    resource_kind: Literal["issue", "pull_request"]
    issue_number: int = Field(ge=1)
    external_actor_id: str = Field(pattern=r"^[1-9][0-9]{0,39}$")
    comment_id: str = Field(pattern=r"^[1-9][0-9]{0,39}$")


class GitHubObservationWakeV1(StrictContract):
    """A durable scheduling request, without comment-processing authority."""

    version: Literal[1]
    state: Literal["pending"]
    source_key: str = Field(pattern=r"^comment:[1-9][0-9]{0,39}$")
    scheduled_epoch: NativeID | None = None
    scan_after_wake: bool = False


class TicketContextReferenceV1(StrictContract):
    receipt_id: NativeID
    sequence: int = Field(ge=1)
    run_id: NativeID
    project_id: NativeID
    component_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    context_digest: Digest
    kind: Literal["scm_receipt", "native_checkpoint_test", "native_checkpoint_maintain"] = "scm_receipt"
    parent_receipt_id: NativeID | None = None
    creator_user_id: NativeID | None = None

    @model_validator(mode="after")
    def typed_lineage(self) -> TicketContextReferenceV1:
        checkpoint = self.kind in {"native_checkpoint_test", "native_checkpoint_maintain"}
        if checkpoint != (self.parent_receipt_id is not None):
            raise ValueError("A checkpoint source reference requires its distinct authenticated parent")
        if self.parent_receipt_id == self.receipt_id:
            raise ValueError("A test creation must retain its own native receipt identity")
        if checkpoint != (self.creator_user_id is not None):
            raise ValueError("Checkpoint source lineage must retain its original creator as provenance")
        return self

    @model_serializer(mode="wrap")
    def retained_reference(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        value: dict[str, Any] = handler(self)
        if value["kind"] == "scm_receipt":
            value.pop("kind")
            value.pop("parent_receipt_id")
            value.pop("creator_user_id")
        return value


class AddressedContextV1(StrictContract):
    version: Literal[1] = 1
    selection: Literal["run", "missing", "ambiguous", "clarify"]
    scopes: list[ConversationReadScopeV1] = Field(min_length=1, max_length=100)
    references: list[TicketContextReferenceV1] = Field(max_length=200)
    candidate_ids: list[NativeID] = Field(max_length=200)
    selected_run_id: NativeID | None
    digest: Digest


def addressed_status(text: str, bot_addresses: tuple[str, ...] = ()) -> AddressedStatusV1 | None:
    """Recognize a bounded status grammar; unsupported addressed requests only clarify."""
    if not text or len(text) > SCM_SOURCE_QUESTION_LIMIT:
        return None
    aliases = {"forge"} | {
        value.lower() for value in bot_addresses if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}", value)
    }
    match = re.match(r"(?:@([a-zA-Z0-9][a-zA-Z0-9_-]{0,99})(?:,)?|(?i:forge),)[ \t]+([^\r\n].*)\Z", text, re.DOTALL)
    if match is None or (match[1] or "forge").lower() not in aliases:
        return None
    message = match[2].casefold()
    normalized = "".join(c for c in unicodedata.normalize("NFD", message) if not unicodedata.combining(c))
    question = normalized.strip(" ?!¿¡.,")
    spanish = bool(re.search(r"\b(?:como|cual|estado|paso|reanuda|ejecucion)\b", question))
    mutation = re.search(
        r"\b(?:resume|restart|retry|deploy|execute|change|delete|cancel|approve|merge|push|"
        r"reanuda|reinicia|reintenta|despliega|ejecuta|cambia|borra|cancela|aprueba)\b",
        question,
    )
    run_request = re.search(r"(?:^|[.?!;]|\band\s+)\s*run\b", question)
    status = re.fullmatch(
        r"(?:how (?:is|are) .+ (?:going|doing)|what(?:'s| is) (?:the )?status(?: of .+)?|"
        r"what happened(?: to .+)?|como (?:va|van|esta|estan)(?: .+)?|"
        r"cual es el estado(?: de .+)?|que paso(?: con .+)?)",
        question,
    )
    return AddressedStatusV1(
        intent="status"
        if status and not mutation and not run_request and not re.search(r"[\r\n;]", question)
        else "clarify",
        language="es" if spanish else "en",
        address=(match[1] or "forge").lower(),
        source_body_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
