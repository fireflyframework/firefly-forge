# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controller-only PostgreSQL records and exact factory backend fence queries."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from firefly_forge.runner_pool_models import FactoryFence


class ForgeRunnerDaemonEntity(BaseEntity):
    __tablename__ = "forge_runner_daemons"
    daemon_id: Mapped[str] = mapped_column(String(128), unique=True)
    profile_id: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeRunnerPoolEntity(BaseEntity):
    __tablename__ = "forge_runner_pools"
    daemon_id: Mapped[UUID] = mapped_column(ForeignKey("forge_runner_daemons.id"))
    profile_id: Mapped[str] = mapped_column(String(64))
    revision: Mapped[str] = mapped_column(String(64), unique=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeRunnerInvocationEntity(BaseEntity):
    __tablename__ = "forge_runner_invocations"
    pool_id: Mapped[UUID] = mapped_column(ForeignKey("forge_runner_pools.id"))
    execution_id: Mapped[str] = mapped_column(String(32), unique=True)
    status: Mapped[str] = mapped_column(String(32))
    owner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    epoch: Mapped[int] = mapped_column(BigInteger, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    admission_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeRunnerEventEntity(BaseEntity):
    __tablename__ = "forge_runner_events"
    __table_args__ = (UniqueConstraint("invocation_id", "sequence"),)
    invocation_id: Mapped[UUID] = mapped_column(ForeignKey("forge_runner_invocations.id"))
    sequence: Mapped[int] = mapped_column(BigInteger)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeRunnerDaemonRepository(Repository[ForgeRunnerDaemonEntity, UUID]):
    async def identity(self, daemon_id: str) -> ForgeRunnerDaemonEntity | None:
        return await self._require_session().scalar(
            select(ForgeRunnerDaemonEntity).where(ForgeRunnerDaemonEntity.daemon_id == daemon_id).with_for_update()
        )


@repository
class ForgeRunnerPoolRepository(Repository[ForgeRunnerPoolEntity, UUID]):
    async def revision(self, value: str) -> ForgeRunnerPoolEntity | None:
        return await self._require_session().scalar(
            select(ForgeRunnerPoolEntity).where(ForgeRunnerPoolEntity.revision == value).with_for_update()
        )


@repository
class ForgeRunnerInvocationRepository(Repository[ForgeRunnerInvocationEntity, UUID]):
    async def execution(self, value: str) -> ForgeRunnerInvocationEntity | None:
        await self._require_session().execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": "forge:runner-invocation:" + value}
        )
        return await self._require_session().scalar(
            select(ForgeRunnerInvocationEntity).where(ForgeRunnerInvocationEntity.execution_id == value)
        )

    async def database_time(self) -> datetime:
        return cast(datetime, await self._require_session().scalar(select(func.clock_timestamp())))

    async def factory_owned(self, fence: FactoryFence) -> bool:
        return bool(
            await self._require_session().scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
                    "WHERE l.locktype='advisory' AND l.mode='ExclusiveLock' AND l.granted AND l.objsubid=1 "
                    "AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database()) "
                    "AND a.pid=:pid AND a.backend_start=:started "
                    "AND l.classid::bigint=((hashtextextended(:key,0)>>32)&4294967295) "
                    "AND l.objid::bigint=(hashtextextended(:key,0)&4294967295))"
                ),
                {
                    "pid": fence.pid,
                    "started": fence.backend_started_at,
                    "key": "forge:execution:" + fence.factory_schema,
                },
            )
        )

    async def rows(
        self, pool_id: UUID, *, execution_id: str | None = None, row_id: UUID | None = None, active_only: bool = False
    ) -> list[ForgeRunnerInvocationEntity]:
        query = select(ForgeRunnerInvocationEntity).where(ForgeRunnerInvocationEntity.pool_id == pool_id)
        if execution_id is not None:
            query = query.where(ForgeRunnerInvocationEntity.execution_id == execution_id)
        if row_id is not None:
            query = query.where(ForgeRunnerInvocationEntity.id == row_id)
        if active_only:
            query = query.where(
                ForgeRunnerInvocationEntity.status.in_(
                    ("queued", "leased", "creating", "created", "starting", "running", "draining", "quarantined")
                )
            )
        return list(
            await self._require_session().scalars(
                query.order_by(ForgeRunnerInvocationEntity.created_at, ForgeRunnerInvocationEntity.id).with_for_update()
            )
        )

    async def daemon_rows(self, daemon_id: UUID) -> list[ForgeRunnerInvocationEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeRunnerInvocationEntity)
                .join(ForgeRunnerPoolEntity)
                .where(
                    ForgeRunnerPoolEntity.daemon_id == daemon_id,
                    ForgeRunnerInvocationEntity.status.in_(
                        ("queued", "leased", "creating", "created", "starting", "running", "draining", "quarantined")
                    ),
                )
                .order_by(ForgeRunnerInvocationEntity.created_at, ForgeRunnerInvocationEntity.id)
                .with_for_update()
            )
        )

    async def renew(self, row_id: UUID, owner: str, epoch: int, seconds: int) -> bool:
        return (
            await self._require_session().scalar(
                update(ForgeRunnerInvocationEntity)
                .where(
                    ForgeRunnerInvocationEntity.id == row_id,
                    ForgeRunnerInvocationEntity.owner_id == owner,
                    ForgeRunnerInvocationEntity.epoch == epoch,
                    ForgeRunnerInvocationEntity.status.in_(
                        ("leased", "creating", "created", "starting", "running", "draining")
                    ),
                    ForgeRunnerInvocationEntity.lease_expires_at > func.clock_timestamp(),
                )
                .values(lease_expires_at=func.clock_timestamp() + timedelta(seconds=seconds))
                .returning(ForgeRunnerInvocationEntity.id)
            )
            is not None
        )


@repository
class ForgeRunnerEventRepository(Repository[ForgeRunnerEventEntity, UUID]):
    async def events(self, invocation_id: UUID) -> list[ForgeRunnerEventEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeRunnerEventEntity)
                .where(ForgeRunnerEventEntity.invocation_id == invocation_id)
                .order_by(ForgeRunnerEventEntity.sequence)
            )
        )
