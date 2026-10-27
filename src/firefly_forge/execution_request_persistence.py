# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Durable API-to-worker control requests and immutable result receipts."""

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import ForeignKey, String, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeExecutionRequestEntity(BaseEntity):
    __tablename__ = "forge_execution_requests"
    __table_args__ = (UniqueConstraint("run_id", "kind", "request_key"),)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("forge_runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    request_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeExecutionRequestEventEntity(BaseEntity):
    __tablename__ = "forge_execution_request_events"
    __table_args__ = (UniqueConstraint("request_id", "sequence"),)
    request_id: Mapped[UUID] = mapped_column(ForeignKey("forge_execution_requests.id"), index=True)
    sequence: Mapped[int] = mapped_column()
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeExecutionRequestRepository(Repository[ForgeExecutionRequestEntity, UUID]):
    async def for_key(self, run_id: UUID, kind: str, request_key: str) -> ForgeExecutionRequestEntity | None:
        return await self._require_session().scalar(
            select(ForgeExecutionRequestEntity).where(
                ForgeExecutionRequestEntity.run_id == run_id,
                ForgeExecutionRequestEntity.kind == kind,
                ForgeExecutionRequestEntity.request_key == request_key,
            )
        )

    async def count_for_run(self, run_id: UUID, kind: str) -> int:
        return int(
            await self._require_session().scalar(
                select(func.count())
                .select_from(ForgeExecutionRequestEntity)
                .where(ForgeExecutionRequestEntity.run_id == run_id, ForgeExecutionRequestEntity.kind == kind)
            )
            or 0
        )

    async def active_for_run(self, run_id: UUID) -> bool:
        return bool(
            await self._require_session().scalar(
                select(func.count())
                .select_from(ForgeExecutionRequestEntity)
                .where(
                    ForgeExecutionRequestEntity.run_id == run_id,
                    ForgeExecutionRequestEntity.status.in_(["queued", "executing"]),
                )
            )
        )

    async def for_run(self, run_id: UUID, kind: str) -> list[ForgeExecutionRequestEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeExecutionRequestEntity)
                .where(
                    ForgeExecutionRequestEntity.run_id == run_id,
                    ForgeExecutionRequestEntity.kind == kind,
                )
                .order_by(ForgeExecutionRequestEntity.created_at.desc())
                .limit(50)
            )
        )

    async def state(
        self, status: str, limit: int = 50, *, kind: str | None = None
    ) -> list[ForgeExecutionRequestEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeExecutionRequestEntity)
                .where(
                    ForgeExecutionRequestEntity.status == status,
                    *([ForgeExecutionRequestEntity.kind == kind] if kind else []),
                )
                .order_by(ForgeExecutionRequestEntity.created_at)
                .limit(limit)
            )
        )

    async def pending_count(self) -> int:
        return int(
            await self._require_session().scalar(
                select(func.count())
                .select_from(ForgeExecutionRequestEntity)
                .where(ForgeExecutionRequestEntity.status.in_(["queued", "executing"]))
            )
            or 0
        )


@repository
class ForgeExecutionRequestEventRepository(Repository[ForgeExecutionRequestEventEntity, UUID]):
    async def events(self, request_id: UUID) -> list[ForgeExecutionRequestEventEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeExecutionRequestEventEntity)
                .where(
                    ForgeExecutionRequestEventEntity.request_id == request_id,
                )
                .order_by(ForgeExecutionRequestEventEntity.sequence)
                .limit(100)
            )
        )
