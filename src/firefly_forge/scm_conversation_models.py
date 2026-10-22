# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Direct status addresses carry read-only intent, never execution options."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Literal

from pydantic import ConfigDict, Field

from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract


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
    transport: Literal["github_rest_poll_v1", "signed_webhook_v1"]
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


class SignedAddressedSourceV1(StrictContract):
    version: Literal[1] = 1
    payload_digest: Digest
    source_digest: Digest
    resource_kind: Literal["issue", "pull_request"]
    issue_number: int = Field(ge=1)
    external_actor_id: str = Field(pattern=r"^[1-9][0-9]{0,39}$")
    comment_id: str = Field(pattern=r"^[1-9][0-9]{0,39}$")


class TicketContextReferenceV1(StrictContract):
    receipt_id: NativeID
    sequence: int = Field(ge=1)
    run_id: NativeID
    project_id: NativeID
    component_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    context_digest: Digest


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
    if not text or len(text) > 40000:
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
