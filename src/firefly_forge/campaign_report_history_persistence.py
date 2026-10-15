# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Immutable native report receipts, separate from campaign execution evidence."""

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeCampaignReportArchiveEntity(BaseEntity):
    __tablename__ = "forge_campaign_report_archives"
    __table_args__ = (
        UniqueConstraint("campaign_id", "request_id"),
        UniqueConstraint("campaign_id", "sequence"),
    )
    project_id: Mapped[UUID] = mapped_column(ForeignKey("forge_projects.id"), index=True)
    campaign_id: Mapped[UUID] = mapped_column(ForeignKey("forge_campaigns.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(32))
    sequence: Mapped[int] = mapped_column()
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeCampaignReportArchiveRepository(Repository[ForgeCampaignReportArchiveEntity, UUID]):
    async def request(self, campaign_id: UUID, request_id: str) -> ForgeCampaignReportArchiveEntity | None:
        return await self._require_session().scalar(
            select(ForgeCampaignReportArchiveEntity).where(
                ForgeCampaignReportArchiveEntity.campaign_id == campaign_id,
                ForgeCampaignReportArchiveEntity.request_id == request_id,
            )
        )
