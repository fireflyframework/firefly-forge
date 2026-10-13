# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native PostgreSQL campaign admissions, immutable attempts and ordered evidence."""

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeCampaignEntity(BaseEntity):
    __tablename__ = "forge_campaigns"
    __table_args__ = (UniqueConstraint("project_id", "request_id"),)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("forge_projects.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeCampaignChildEntity(BaseEntity):
    __tablename__ = "forge_campaign_children"
    __table_args__ = (UniqueConstraint("campaign_id", "component_id", "attempt"), UniqueConstraint("run_id"))
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("forge_campaigns.id"), index=True)
    component_id: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column()
    run_id: Mapped[UUID] = mapped_column(ForeignKey("forge_runs.id"))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeCampaignEventEntity(BaseEntity):
    __tablename__ = "forge_campaign_events"
    __table_args__ = (UniqueConstraint("campaign_id", "sequence"), UniqueConstraint("campaign_id", "key"))
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("forge_campaigns.id"), index=True)
    sequence: Mapped[int] = mapped_column()
    key: Mapped[str] = mapped_column(String(160))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeCampaignRepository(Repository[ForgeCampaignEntity, UUID]):
    async def request(self, project_id: UUID, request_id: str) -> ForgeCampaignEntity | None:
        return await self._require_session().scalar(
            select(ForgeCampaignEntity).where(
                ForgeCampaignEntity.project_id == project_id, ForgeCampaignEntity.request_id == request_id
            )
        )

    async def latest(self, project_id: UUID | None = None) -> list[ForgeCampaignEntity]:
        statement = select(ForgeCampaignEntity).order_by(ForgeCampaignEntity.created_at.desc()).limit(200)
        if project_id is not None:
            statement = statement.where(ForgeCampaignEntity.project_id == project_id)
        return list(await self._require_session().scalars(statement))


@repository
class ForgeCampaignChildRepository(Repository[ForgeCampaignChildEntity, UUID]):
    async def children(self, campaign_id: UUID) -> list[ForgeCampaignChildEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeCampaignChildEntity)
                .where(ForgeCampaignChildEntity.campaign_id == campaign_id)
                .order_by(ForgeCampaignChildEntity.component_id, ForgeCampaignChildEntity.attempt)
            )
        )


@repository
class ForgeCampaignEventRepository(Repository[ForgeCampaignEventEntity, UUID]):
    async def events(self, campaign_id: UUID) -> list[ForgeCampaignEventEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeCampaignEventEntity)
                .where(ForgeCampaignEventEntity.campaign_id == campaign_id)
                .order_by(ForgeCampaignEventEntity.sequence)
            )
        )
