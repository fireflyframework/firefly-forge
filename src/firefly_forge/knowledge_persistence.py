# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native project admission records independent of execution-run history."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeKnowledgeUploadEntity(BaseEntity):
    __tablename__ = "forge_knowledge_uploads"
    __table_args__ = (UniqueConstraint("repository_id", "digest"),)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("forge_repositories.id"), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeKnowledgeUploadRepository(Repository[ForgeKnowledgeUploadEntity, UUID]):
    async def find_digest(self, repository_id: UUID, digest: str) -> ForgeKnowledgeUploadEntity | None:
        return await self._require_session().scalar(
            select(ForgeKnowledgeUploadEntity).where(
                ForgeKnowledgeUploadEntity.repository_id == repository_id, ForgeKnowledgeUploadEntity.digest == digest
            )
        )

    async def latest(self, repository_id: UUID) -> list[ForgeKnowledgeUploadEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeKnowledgeUploadEntity)
                .where(ForgeKnowledgeUploadEntity.repository_id == repository_id)
                .order_by(ForgeKnowledgeUploadEntity.created_at.desc())
                .limit(100)
            )
        )
