# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native immutable reporting-plan revisions, independent of execution journals."""

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeCampaignDeliveryPlanEntity(BaseEntity):
    __tablename__ = "forge_campaign_delivery_plan_revisions"
    __table_args__ = (UniqueConstraint("campaign_id", "revision"), UniqueConstraint("campaign_id", "request_id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("forge_projects.id"), index=True)
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("forge_campaigns.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column()
    previous_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    digest: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeCampaignDeliveryPlanRepository(Repository[ForgeCampaignDeliveryPlanEntity, UUID]):
    async def request(self, campaign_id: UUID, request_id: str) -> ForgeCampaignDeliveryPlanEntity | None:
        return await self._require_session().scalar(
            select(ForgeCampaignDeliveryPlanEntity).where(
                ForgeCampaignDeliveryPlanEntity.campaign_id == campaign_id,
                ForgeCampaignDeliveryPlanEntity.request_id == request_id,
            )
        )
