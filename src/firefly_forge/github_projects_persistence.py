# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native binding heads and immutable Projects configuration receipts."""

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeGitHubProjectsBindingEntity(BaseEntity):
    __tablename__ = "forge_github_projects_bindings"
    __table_args__ = (UniqueConstraint("project_id", "repository_id"),)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("forge_projects.id"), index=True)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("forge_repositories.id"), index=True)
    revision: Mapped[int] = mapped_column()
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeGitHubProjectsConfigurationEntity(BaseEntity):
    __tablename__ = "forge_github_projects_configuration_receipts"
    __table_args__ = (
        UniqueConstraint("project_id", "repository_id", "revision"),
        UniqueConstraint("project_id", "repository_id", "request_id"),
    )
    project_id: Mapped[UUID] = mapped_column(ForeignKey("forge_projects.id"), index=True)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("forge_repositories.id"), index=True)
    revision: Mapped[int] = mapped_column()
    request_id: Mapped[UUID] = mapped_column()
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeGitHubProjectsCreationEntity(BaseEntity):
    __tablename__ = "forge_github_projects_creations"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("forge_projects.id"), index=True)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("forge_repositories.id"), index=True)
    scope_digest: Mapped[str] = mapped_column(String(64), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeGitHubProjectsCreationReceiptEntity(BaseEntity):
    __tablename__ = "forge_github_projects_creation_receipts"
    creation_id: Mapped[UUID] = mapped_column(ForeignKey("forge_github_projects_creations.id"), unique=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeGitHubProjectsBindingRepository(Repository[ForgeGitHubProjectsBindingEntity, UUID]):
    async def scoped(self, project_id: UUID, repository_id: UUID) -> ForgeGitHubProjectsBindingEntity | None:
        return await self._require_session().scalar(
            select(ForgeGitHubProjectsBindingEntity)
            .where(
                ForgeGitHubProjectsBindingEntity.project_id == project_id,
                ForgeGitHubProjectsBindingEntity.repository_id == repository_id,
            )
            .with_for_update()
        )
