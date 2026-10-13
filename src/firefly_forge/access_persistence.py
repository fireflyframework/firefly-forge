# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native project membership and immutable permission-change receipts."""

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeMembershipEntity(BaseEntity):
    __tablename__ = "forge_project_memberships"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id"),
        CheckConstraint("role IN ('owner','maintainer','developer','reviewer','viewer')"),
    )
    project_id: Mapped[UUID] = mapped_column(ForeignKey("forge_projects.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("forge_identity_users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(default=True)
    revision: Mapped[int] = mapped_column(default=1)


class ForgeAccessEventEntity(BaseEntity):
    __tablename__ = "forge_access_events"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("forge_projects.id"), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeMembershipRepository(Repository[ForgeMembershipEntity, UUID]):
    async def for_user(self, user_id: UUID) -> list[ForgeMembershipEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeMembershipEntity).where(
                    ForgeMembershipEntity.user_id == user_id,
                    ForgeMembershipEntity.active.is_(True),
                )
            )
        )

    async def for_project(self, project_id: UUID) -> list[ForgeMembershipEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeMembershipEntity)
                .where(
                    ForgeMembershipEntity.project_id == project_id,
                )
                .with_for_update()
            )
        )


@repository
class ForgeAccessEventRepository(Repository[ForgeAccessEventEntity, UUID]):
    pass
