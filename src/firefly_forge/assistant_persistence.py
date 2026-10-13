# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Product-design conversations, request receipts and immutable write-ahead events."""

from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeAssistantSessionEntity(BaseEntity):
    __tablename__ = "forge_assistant_sessions"
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("forge_repositories.id"), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeAssistantTurnEntity(BaseEntity):
    __tablename__ = "forge_assistant_turns"
    __table_args__ = (UniqueConstraint("session_id", "request_id"),)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("forge_assistant_sessions.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    owner_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    claim_epoch: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeAssistantEventEntity(BaseEntity):
    __tablename__ = "forge_assistant_events"
    __table_args__ = (UniqueConstraint("session_id", "sequence"),)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("forge_assistant_sessions.id"), index=True)
    sequence: Mapped[int] = mapped_column()
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeAssistantSessionRepository(Repository[ForgeAssistantSessionEntity, UUID]):
    async def latest(self, repository_id: UUID) -> list[ForgeAssistantSessionEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeAssistantSessionEntity)
                .where(ForgeAssistantSessionEntity.repository_id == repository_id)
                .order_by(ForgeAssistantSessionEntity.created_at.desc())
                .limit(50)
            )
        )


@repository
class ForgeAssistantTurnRepository(Repository[ForgeAssistantTurnEntity, UUID]):
    async def database_time(self) -> datetime:
        return cast(datetime, await self._require_session().scalar(select(func.clock_timestamp())))

    async def renew_claim(self, turn_id: UUID, owner_id: str, epoch: int, lease_seconds: int) -> bool:
        return (
            await self._require_session().scalar(
                update(ForgeAssistantTurnEntity)
                .where(
                    ForgeAssistantTurnEntity.id == turn_id,
                    ForgeAssistantTurnEntity.status == "processing",
                    ForgeAssistantTurnEntity.owner_id == owner_id,
                    ForgeAssistantTurnEntity.claim_epoch == epoch,
                    ForgeAssistantTurnEntity.lease_expires_at > func.clock_timestamp(),
                )
                .values(
                    lease_expires_at=func.clock_timestamp() + timedelta(seconds=lease_seconds),
                    last_heartbeat_at=func.clock_timestamp(),
                )
                .returning(ForgeAssistantTurnEntity.id)
            )
            is not None
        )

    async def latest(self, session_id: UUID) -> list[ForgeAssistantTurnEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeAssistantTurnEntity)
                .where(ForgeAssistantTurnEntity.session_id == session_id)
                .order_by(ForgeAssistantTurnEntity.created_at.asc())
                .limit(50)
            )
        )

    async def request(self, session_id: UUID, request_id: str) -> ForgeAssistantTurnEntity | None:
        return await self._require_session().scalar(
            select(ForgeAssistantTurnEntity).where(
                ForgeAssistantTurnEntity.session_id == session_id, ForgeAssistantTurnEntity.request_id == request_id
            )
        )


@repository
class ForgeAssistantEventRepository(Repository[ForgeAssistantEventEntity, UUID]):
    async def events(self, session_id: UUID) -> list[ForgeAssistantEventEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeAssistantEventEntity)
                .where(ForgeAssistantEventEntity.session_id == session_id)
                .order_by(ForgeAssistantEventEntity.sequence)
                .limit(300)
            )
        )
