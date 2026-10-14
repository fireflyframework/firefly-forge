# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded conversation uploads, immutable receipts and hash-checked private model selections."""

from __future__ import annotations

import base64
import binascii
import builtins
import copy
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode

from firefly_forge.assistant_attachment_persistence import (
    ForgeAssistantAttachmentEntity,
    ForgeAssistantAttachmentRepository,
)
from firefly_forge.assistant_persistence import ForgeAssistantSessionEntity, ForgeAssistantSessionRepository
from firefly_forge.attachment_decode import DOCUMENT_TYPES, IMAGE_TYPES, MAX_BYTES, MAX_TEXT, decode_attachment
from firefly_forge.project_access import CURRENT_PRINCIPAL, Permission, ProjectAccess
from firefly_forge.secrets import contains_known_credential
from firefly_forge.store import Store, identifier, now

Data = dict[str, Any]
Decoder = Callable[[str, bytes], Awaitable[Data]]
PUBLIC_FIELDS = {
    "id",
    "request_id",
    "filename",
    "mime_type",
    "kind",
    "content_bytes",
    "sha256",
    "status",
    "text_chars",
    "truncated",
    "created_at",
    "detail",
}


class AttachmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    filename: str = Field(min_length=1, max_length=180)
    content_base64: str = Field(min_length=4, max_length=1_333_336)

    @field_validator("filename")
    @classmethod
    def bounded_name(cls, value: str) -> str:
        if (
            not value[0].isalnum()
            or "/" in value
            or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("Use an ordinary filename without paths or control characters")
        return value


@service
class AssistantAttachments:
    def __init__(
        self,
        store: Store,
        sessions: ForgeAssistantSessionRepository | None = None,
        records: ForgeAssistantAttachmentRepository | None = None,
        decoder: Decoder | None = None,
    ) -> None:
        self.store = store
        self.sessions = sessions or ForgeAssistantSessionRepository()
        self.records = records or ForgeAssistantAttachmentRepository()
        self.decoder = decoder or decode_attachment
        self.credential_registry: Callable[[], dict[str, str]] = dict

    async def _guard(self, value: Any) -> None:
        await self.store.admit_credentials(value)
        if contains_known_credential(value, self.credential_registry()):
            raise ValueError("Remove configured credentials from conversation attachments")

    async def _parent(
        self, repository_id: str, session_id: str, *, write: bool = False, lock: bool = False
    ) -> ForgeAssistantSessionEntity:
        row = await self.sessions.find_by_id(identifier(session_id), lock=LockMode.PESSIMISTIC_WRITE if lock else None)
        if row is None or row.repository_id != identifier(repository_id):
            raise KeyError("Conversation not found in this repository")
        actor = CURRENT_PRINCIPAL.get()
        if actor is not None:
            access = ProjectAccess(self.store)
            permission: Permission = "edit" if write else "read"
            if row.data.get("project_id") or access.is_admin(actor):
                await access.authorize_actor(
                    ProjectAccess.actor_snapshot(actor), row.data.get("project_id"), permission
                )
            else:
                await access.require_repository(actor, repository_id, permission)
        return row

    async def _public(self, row: ForgeAssistantAttachmentEntity) -> Data:
        result = {key: copy.deepcopy(value) for key, value in row.data.items() if key in PUBLIC_FIELDS}
        if any(
            result.get(key) != value
            for key, value in {
                "id": row.id.hex,
                "request_id": row.request_id,
                "filename": row.filename,
                "mime_type": row.mime_type,
                "kind": row.kind,
                "status": row.status,
                "sha256": row.sha256,
            }.items()
        ):
            raise ValueError("Attachment metadata integrity check failed")
        await self._guard(result)
        return result

    async def _verified(self, row: ForgeAssistantAttachmentEntity) -> Data:
        if row.status != "ready":
            raise ValueError("Only ready attachments can enter model context")
        if (
            not row.raw_bytes
            or len(row.raw_bytes) > MAX_BYTES
            or hashlib.sha256(row.raw_bytes).hexdigest() != row.sha256
        ):
            raise ValueError("Attachment source hash check failed")
        if row.kind == "image":
            if (
                not row.canonical_image
                or len(row.canonical_image) > MAX_BYTES
                or hashlib.sha256(row.canonical_image).hexdigest() != row.image_sha256
            ):
                raise ValueError("Attachment image hash check failed")
        elif row.kind != "document" or row.canonical_image is not None or row.image_sha256 is not None:
            raise ValueError("Attachment content type integrity check failed")
        result = await self._public(row)
        if (
            len(row.extracted_text) > MAX_TEXT
            or result["text_chars"] != len(row.extracted_text)
            or result["content_bytes"] != len(row.raw_bytes)
            or row.data.get("text_sha256") != hashlib.sha256(row.extracted_text.encode()).hexdigest()
        ):
            raise ValueError("Attachment text hash, limits or metadata do not match")
        await self._guard([row.extracted_text, row.raw_bytes.decode("utf8", errors="ignore")])
        return result

    async def _replay(self, row: ForgeAssistantAttachmentEntity, filename: str, sha256: str) -> Data:
        if row.filename != filename or row.sha256 != sha256:
            raise ValueError("Attachment replay conflicts with the original filename or content")
        return await self._verified(row)

    async def admit(self, repository_id: str, session_id: str, request: AttachmentInput) -> Data:
        request = AttachmentInput.model_validate(request)
        await self._guard(request.filename)
        try:
            raw = base64.b64decode(request.content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Upload valid base64 file content") from None
        if not raw or len(raw) > MAX_BYTES:
            raise ValueError("Upload a nonempty file of at most 1 MB")
        await self._guard(raw.decode("utf8", errors="ignore"))
        sha256 = hashlib.sha256(raw).hexdigest()
        async with self.store._unit():
            parent = await self._parent(repository_id, session_id, write=True)
            existing = await self.records.request(parent.id, request.request_id)
            if existing is not None:
                return await self._replay(existing, request.filename, sha256)
            if await self.records.count_for_session(parent.id) >= 20:
                raise ValueError("This conversation already contains twenty attachments")
        try:
            decoded = await self.decoder(request.filename, raw)
        except (ValueError, TimeoutError):
            raise ValueError(
                "This attachment could not be read within the supported format and processing limits"
            ) from None
        kind, mime, content, image = (decoded.get(key) for key in ("kind", "mime_type", "text", "image"))
        truncated = decoded.get("truncated")
        supported = set(DOCUMENT_TYPES.values()) if kind == "document" else {value[1] for value in IMAGE_TYPES.values()}
        if (
            not isinstance(kind, str)
            or kind not in {"document", "image"}
            or not isinstance(mime, str)
            or mime not in supported
            or not isinstance(content, str)
            or type(truncated) is not bool
            or kind == "document"
            and image is not None
            or kind == "image"
            and (not isinstance(image, bytes) or not image or len(image) > MAX_BYTES)
        ):
            raise ValueError("Attachment decoder returned invalid bounded content")
        await self._guard(content)
        identifier_value = uuid4()
        metadata = {
            "id": identifier_value.hex,
            "request_id": request.request_id,
            "filename": request.filename,
            "mime_type": mime,
            "kind": kind,
            "content_bytes": len(raw),
            "sha256": sha256,
            "status": "ready",
            "text_chars": min(len(content), MAX_TEXT),
            "truncated": truncated or len(content) > MAX_TEXT,
            "created_at": now(),
            "text_sha256": hashlib.sha256(content[:MAX_TEXT].encode()).hexdigest(),
        }
        await self._guard(metadata)
        async with self.store._unit():
            parent = await self._parent(repository_id, session_id, write=True, lock=True)
            existing = await self.records.request(parent.id, request.request_id)
            if existing is not None:
                return await self._replay(existing, request.filename, sha256)
            if await self.records.count_for_session(parent.id) >= 20:
                raise ValueError("This conversation already contains twenty attachments")
            await self._guard([metadata, content, raw.decode("utf8", errors="ignore")])
            row = ForgeAssistantAttachmentEntity(
                id=identifier_value,
                session_id=parent.id,
                request_id=request.request_id,
                filename=request.filename,
                mime_type=mime,
                kind=kind,
                status="ready",
                sha256=sha256,
                raw_bytes=raw,
                extracted_text=content[:MAX_TEXT],
                canonical_image=image,
                image_sha256=hashlib.sha256(image).hexdigest() if image else None,
                data=metadata,
            )
            await self.records.save(row)
            return await self._public(row)

    async def list(self, repository_id: str, session_id: str) -> list[Data]:
        async with self.store._unit():
            parent = await self._parent(repository_id, session_id)
            return [await self._public(row) for row in await self.records.for_session(parent.id)]

    async def get(self, repository_id: str, session_id: str, attachment_id: str) -> Data:
        async with self.store._unit():
            parent = await self._parent(repository_id, session_id)
            selected = await self.records.metadata(parent.id, identifier(attachment_id))
            if selected is None:
                raise KeyError("Attachment not found in this conversation")
            return await self._public(selected)

    async def freeze(self, repository_id: str, session_id: str, ids: builtins.list[str]) -> builtins.list[Data]:
        if len(ids) > 8 or len(set(ids)) != len(ids):
            raise ValueError("Select at most eight unique conversation attachments")
        identifiers = [identifier(value) for value in ids]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Select distinct attachment identifiers")
        async with self.store._unit():
            parent = await self._parent(repository_id, session_id)
            selected = {row.id: row for row in await self.records.selected(parent.id, identifiers)}
            if len(selected) != len(identifiers):
                raise KeyError("Attachment not found in this conversation")
            result = []
            for key in identifiers:
                row = selected[key]
                result.append(
                    {
                        **(await self._verified(row)),
                        "text": row.extracted_text,
                        "image": row.canonical_image,
                        "image_sha256": row.image_sha256,
                        "raw_bytes": row.raw_bytes,
                    }
                )
            return result
