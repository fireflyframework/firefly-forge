# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owned runner execution intents keyed to a durable run stage and invocation slot."""

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeSandboxEntity(BaseEntity):
    __tablename__ = "forge_sandbox_executions"
    __table_args__ = (UniqueConstraint("run_id", "task_id", "parent_attempt", "ordinal"),)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("forge_runs.id"), index=True)
    task_id: Mapped[str] = mapped_column(String(32))
    parent_attempt: Mapped[int] = mapped_column()
    ordinal: Mapped[int] = mapped_column()
    container_name: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    snapshot_digest: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeSandboxRepository(Repository[ForgeSandboxEntity, UUID]):
    async def unfinished(self) -> list[ForgeSandboxEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeSandboxEntity)
                .where(ForgeSandboxEntity.status != "removed")
                .order_by(ForgeSandboxEntity.created_at)
                .limit(500)
            )
        )

    async def slot(self, run_id: UUID, task_id: str, parent_attempt: int, ordinal: int) -> ForgeSandboxEntity | None:
        return await self._require_session().scalar(
            select(ForgeSandboxEntity).where(
                ForgeSandboxEntity.run_id == run_id,
                ForgeSandboxEntity.task_id == task_id,
                ForgeSandboxEntity.parent_attempt == parent_attempt,
                ForgeSandboxEntity.ordinal == ordinal,
            )
        )

    async def for_run(self, run_id: UUID) -> list[ForgeSandboxEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeSandboxEntity)
                .where(ForgeSandboxEntity.run_id == run_id)
                .order_by(ForgeSandboxEntity.created_at)
                .limit(500)
            )
        )
