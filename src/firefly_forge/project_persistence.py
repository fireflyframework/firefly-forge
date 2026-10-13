# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Product-level configuration and immutable reverse-engineering evidence."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeProjectEntity(BaseEntity):
    __tablename__ = "forge_projects"
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeProjectRevisionEntity(BaseEntity):
    __tablename__ = "forge_project_revisions"
    __table_args__ = (UniqueConstraint("project_id", "revision"),)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("forge_projects.id"), index=True)
    revision: Mapped[int] = mapped_column()
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeAnalysisEntity(BaseEntity):
    __tablename__ = "forge_analyses"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("forge_projects.id"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeProjectRepository(Repository[ForgeProjectEntity, UUID]):
    async def latest(self) -> list[ForgeProjectEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeProjectEntity).order_by(ForgeProjectEntity.created_at.desc()).limit(200)
            )
        )


@repository
class ForgeProjectRevisionRepository(Repository[ForgeProjectRevisionEntity, UUID]):
    async def revision(self, project_id: UUID, revision: int) -> ForgeProjectRevisionEntity | None:
        return await self._require_session().scalar(
            select(ForgeProjectRevisionEntity).where(
                ForgeProjectRevisionEntity.project_id == project_id,
                ForgeProjectRevisionEntity.revision == revision,
            )
        )


@repository
class ForgeAnalysisRepository(Repository[ForgeAnalysisEntity, UUID]):
    async def latest(self, project_id: UUID) -> list[ForgeAnalysisEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeAnalysisEntity)
                .where(ForgeAnalysisEntity.project_id == project_id)
                .order_by(ForgeAnalysisEntity.created_at.desc())
                .limit(50)
            )
        )
