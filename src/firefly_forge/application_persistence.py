# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native repositories for immutable operational samples and incident transitions."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import BigInteger, Identity, Index, String, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeObservationEntity(BaseEntity):
    __tablename__ = "forge_observations"
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    application_id: Mapped[str] = mapped_column(String(64), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeIncidentEntity(BaseEntity):
    __tablename__ = "forge_incidents"
    application_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    __table_args__ = (
        Index("uq_forge_open_incident", "application_id", unique=True, postgresql_where=text("status = 'open'")),
    )


@repository
class ForgeObservationRepository(Repository[ForgeObservationEntity, UUID]):
    async def for_application(self, application_id: str) -> list[ForgeObservationEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeObservationEntity)
                .where(ForgeObservationEntity.application_id == application_id)
                .order_by(ForgeObservationEntity.sequence.desc())
                .limit(100)
            )
        )


@repository
class ForgeIncidentRepository(Repository[ForgeIncidentEntity, UUID]):
    async def latest(self, application_id: str | None = None) -> list[ForgeIncidentEntity]:
        statement = select(ForgeIncidentEntity).order_by(ForgeIncidentEntity.created_at.desc()).limit(500)
        if application_id:
            statement = statement.where(ForgeIncidentEntity.application_id == application_id)
        return list(await self._require_session().scalars(statement))

    async def open_for(self, application_id: str) -> ForgeIncidentEntity | None:
        return await self._require_session().scalar(
            select(ForgeIncidentEntity).where(
                ForgeIncidentEntity.application_id == application_id, ForgeIncidentEntity.status == "open"
            )
        )
