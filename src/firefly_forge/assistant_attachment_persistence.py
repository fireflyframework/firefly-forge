# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Immutable, conversation-scoped attachment content and upload receipts."""

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import ForeignKey, LargeBinary, String, Text, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, load_only, mapped_column


class ForgeAssistantAttachmentEntity(BaseEntity):
    __tablename__ = "forge_assistant_attachments"
    __table_args__ = (UniqueConstraint("session_id", "request_id"),)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("forge_assistant_sessions.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(32))
    filename: Mapped[str] = mapped_column(String(180))
    mime_type: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    sha256: Mapped[str] = mapped_column(String(64))
    raw_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    extracted_text: Mapped[str] = mapped_column(Text)
    canonical_image: Mapped[bytes | None] = mapped_column(LargeBinary)
    image_sha256: Mapped[str | None] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeAssistantAttachmentRepository(Repository[ForgeAssistantAttachmentEntity, UUID]):
    async def request(self, session_id: UUID, request_id: str) -> ForgeAssistantAttachmentEntity | None:
        return await self._require_session().scalar(
            select(ForgeAssistantAttachmentEntity).where(
                ForgeAssistantAttachmentEntity.session_id == session_id,
                ForgeAssistantAttachmentEntity.request_id == request_id,
            )
        )

    async def for_session(self, session_id: UUID) -> list[ForgeAssistantAttachmentEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeAssistantAttachmentEntity)
                .options(
                    load_only(
                        ForgeAssistantAttachmentEntity.id,
                        ForgeAssistantAttachmentEntity.session_id,
                        ForgeAssistantAttachmentEntity.request_id,
                        ForgeAssistantAttachmentEntity.filename,
                        ForgeAssistantAttachmentEntity.mime_type,
                        ForgeAssistantAttachmentEntity.kind,
                        ForgeAssistantAttachmentEntity.status,
                        ForgeAssistantAttachmentEntity.sha256,
                        ForgeAssistantAttachmentEntity.data,
                    )
                )
                .where(ForgeAssistantAttachmentEntity.session_id == session_id)
                .order_by(ForgeAssistantAttachmentEntity.created_at, ForgeAssistantAttachmentEntity.id)
                .limit(20)
            )
        )

    async def metadata(self, session_id: UUID, attachment_id: UUID) -> ForgeAssistantAttachmentEntity | None:
        return next((row for row in await self.for_session(session_id) if row.id == attachment_id), None)

    async def count_for_session(self, session_id: UUID) -> int:
        return int(
            await self._require_session().scalar(
                select(func.count())
                .select_from(ForgeAssistantAttachmentEntity)
                .where(ForgeAssistantAttachmentEntity.session_id == session_id)
            )
            or 0
        )

    async def selected(self, session_id: UUID, ids: list[UUID]) -> list[ForgeAssistantAttachmentEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeAssistantAttachmentEntity).where(
                    ForgeAssistantAttachmentEntity.session_id == session_id,
                    ForgeAssistantAttachmentEntity.id.in_(ids),
                )
            )
        )
