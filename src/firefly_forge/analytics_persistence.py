# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native analytics binding heads and immutable administrator change receipts."""

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import ForeignKey, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeAnalyticsBindingEntity(BaseEntity):
    __tablename__ = "forge_analytics_bindings"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("forge_projects.id"), unique=True)
    revision: Mapped[int] = mapped_column()
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeAnalyticsConfigurationReceiptEntity(BaseEntity):
    __tablename__ = "forge_analytics_configuration_receipts"
    __table_args__ = (UniqueConstraint("project_id", "revision"),)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("forge_projects.id"), index=True)
    revision: Mapped[int] = mapped_column()
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeAnalyticsBindingRepository(Repository[ForgeAnalyticsBindingEntity, UUID]):
    async def for_project(self, project_id: UUID) -> ForgeAnalyticsBindingEntity | None:
        return await self._require_session().scalar(
            select(ForgeAnalyticsBindingEntity)
            .where(ForgeAnalyticsBindingEntity.project_id == project_id)
            .with_for_update()
        )
