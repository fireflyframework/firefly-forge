# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native durable tool invocations; the run lock serializes admission and budgets."""

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeToolInvocationEntity(BaseEntity):
    __tablename__ = "forge_tool_invocations"
    __table_args__ = (
        UniqueConstraint("run_id", "correlation_id"),
        CheckConstraint("status IN ('pending','unknown','succeeded','rejected','abandoned')"),
    )
    run_id: Mapped[UUID] = mapped_column(ForeignKey("forge_runs.id"), index=True)
    task_id: Mapped[str] = mapped_column(String(80))
    tool: Mapped[str] = mapped_column(String(160))
    correlation_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeToolInvocationRepository(Repository[ForgeToolInvocationEntity, UUID]):
    async def invocation(self, run_id: UUID, correlation_id: str) -> ForgeToolInvocationEntity | None:
        return await self._require_session().scalar(
            select(ForgeToolInvocationEntity).where(
                ForgeToolInvocationEntity.run_id == run_id,
                ForgeToolInvocationEntity.correlation_id == correlation_id,
            )
        )

    async def for_run(self, run_id: UUID) -> list[ForgeToolInvocationEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeToolInvocationEntity)
                .where(ForgeToolInvocationEntity.run_id == run_id)
                .order_by(ForgeToolInvocationEntity.created_at, ForgeToolInvocationEntity.id)
            )
        )
