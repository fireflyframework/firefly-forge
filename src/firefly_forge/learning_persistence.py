# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PostgreSQL learning authority, immutable evidence events and bounded projections."""

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeLearningRecordEntity(BaseEntity):
    __tablename__ = "forge_learning_records"
    __table_args__ = (
        UniqueConstraint("run_id", "correlation_id"),
        CheckConstraint("state IN ('candidate','verified','invalidated','superseded')"),
    )
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("forge_repositories.id"), index=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("forge_runs.id"), index=True)
    correlation_id: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeLearningEventEntity(BaseEntity):
    __tablename__ = "forge_learning_events"
    __table_args__ = (UniqueConstraint("record_id", "sequence"), UniqueConstraint("record_id", "request_id"))
    record_id: Mapped[UUID] = mapped_column(ForeignKey("forge_learning_records.id"), index=True)
    sequence: Mapped[int] = mapped_column(BigInteger)
    request_id: Mapped[str] = mapped_column(String(32))
    digest: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeLearningProjectionEntity(BaseEntity):
    __tablename__ = "forge_learning_projections"
    __table_args__ = (
        UniqueConstraint("record_id"),
        CheckConstraint("state IN ('pending','executing','source_admitted','verified','unknown','abandoned')"),
    )
    record_id: Mapped[UUID] = mapped_column(ForeignKey("forge_learning_records.id"), index=True)
    state: Mapped[str] = mapped_column(String(20))
    payload_digest: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeLearningRepository(Repository[ForgeLearningRecordEntity, UUID]):
    async def database_time(self) -> datetime:
        return cast(datetime, await self._require_session().scalar(select(func.clock_timestamp())))

    async def for_repository(self, repository_id: UUID) -> list[ForgeLearningRecordEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeLearningRecordEntity)
                .where(ForgeLearningRecordEntity.repository_id == repository_id)
                .order_by(ForgeLearningRecordEntity.created_at.desc(), ForgeLearningRecordEntity.id)
                .limit(256)
            )
        )

    async def for_run(self, run_id: UUID) -> list[ForgeLearningRecordEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeLearningRecordEntity)
                .where(ForgeLearningRecordEntity.run_id == run_id)
                .order_by(ForgeLearningRecordEntity.created_at, ForgeLearningRecordEntity.id)
                .limit(100)
            )
        )


@repository
class ForgeLearningEventRepository(Repository[ForgeLearningEventEntity, UUID]):
    async def for_request(self, record_id: UUID, request_id: str) -> ForgeLearningEventEntity | None:
        return await self._require_session().scalar(
            select(ForgeLearningEventEntity).where(
                ForgeLearningEventEntity.record_id == record_id, ForgeLearningEventEntity.request_id == request_id
            )
        )

    async def for_record(self, record_id: UUID) -> list[ForgeLearningEventEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeLearningEventEntity)
                .where(ForgeLearningEventEntity.record_id == record_id)
                .order_by(ForgeLearningEventEntity.sequence)
            )
        )


@repository
class ForgeLearningProjectionRepository(Repository[ForgeLearningProjectionEntity, UUID]):
    async def for_record(self, record_id: UUID) -> ForgeLearningProjectionEntity | None:
        return await self._require_session().scalar(
            select(ForgeLearningProjectionEntity).where(ForgeLearningProjectionEntity.record_id == record_id)
        )
