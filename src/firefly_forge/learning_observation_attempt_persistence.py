# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native request claims with immutable origin and terminal observation decisions."""

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column


class ForgeLearningObservationAttemptEntity(BaseEntity):
    __tablename__ = "forge_learning_observation_attempts"
    __table_args__ = (
        UniqueConstraint("record_id", "attempt_id"),
        CheckConstraint("state IN ('preparing','dispatched','not_dispatched','verified','unknown')"),
    )
    record_id: Mapped[UUID] = mapped_column(ForeignKey("forge_learning_records.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(String(37))
    state: Mapped[str] = mapped_column(String(20))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeLearningObservationAttemptRepository(Repository[ForgeLearningObservationAttemptEntity, UUID]):
    async def for_attempt(
        self, record_id: UUID, attempt_id: str, *, locked: bool = False
    ) -> ForgeLearningObservationAttemptEntity | None:
        statement = select(ForgeLearningObservationAttemptEntity).where(
            ForgeLearningObservationAttemptEntity.record_id == record_id,
            ForgeLearningObservationAttemptEntity.attempt_id == attempt_id,
        )
        if locked:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return await self._require_session().scalar(statement)

    async def count_for_record(self, record_id: UUID) -> int:
        return int(
            await self._require_session().scalar(
                select(func.count())
                .select_from(ForgeLearningObservationAttemptEntity)
                .where(ForgeLearningObservationAttemptEntity.record_id == record_id)
            )
            or 0
        )

    async def for_record(self, record_id: UUID) -> list[ForgeLearningObservationAttemptEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeLearningObservationAttemptEntity)
                .where(ForgeLearningObservationAttemptEntity.record_id == record_id)
                .order_by(ForgeLearningObservationAttemptEntity.created_at, ForgeLearningObservationAttemptEntity.id)
                .limit(100)
            )
        )


async def observation_attempt_guards(session: AsyncSession) -> None:
    await session.execute(
        text("""
        CREATE OR REPLACE FUNCTION forge_guard_observation_attempt() RETURNS trigger AS $$
        BEGIN
          IF OLD.id IS DISTINCT FROM NEW.id OR OLD.record_id IS DISTINCT FROM NEW.record_id
            OR OLD.attempt_id IS DISTINCT FROM NEW.attempt_id OR OLD.created_at IS DISTINCT FROM NEW.created_at
            OR (OLD.data - ARRAY['state','disposition','finished_at','observation','digest']) IS DISTINCT FROM
               (NEW.data - ARRAY['state','disposition','finished_at','observation','digest'])
            OR NEW.data->>'state' IS DISTINCT FROM NEW.state
            OR (OLD.state IN ('not_dispatched','verified','unknown') AND
                (OLD.state IS DISTINCT FROM NEW.state OR OLD.data IS DISTINCT FROM NEW.data))
            OR (OLD.state=NEW.state AND OLD.data IS DISTINCT FROM NEW.data) THEN
            RAISE EXCEPTION 'Observation attempt origin and terminal receipt are immutable';
          END IF;
          IF OLD.state IS DISTINCT FROM NEW.state AND NOT (
            (OLD.state='preparing' AND NEW.state IN ('dispatched','not_dispatched')) OR
            (OLD.state='dispatched' AND NEW.state IN ('verified','unknown'))
          ) THEN RAISE EXCEPTION 'Observation attempt transition cannot replay a request'; END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql
        """)
    )
    await session.execute(
        text(
            "CREATE OR REPLACE TRIGGER forge_observation_attempt_admission BEFORE UPDATE ON "
            "forge_learning_observation_attempts FOR EACH ROW EXECUTE FUNCTION forge_guard_observation_attempt()"
        )
    )
    await session.execute(
        text(
            "CREATE OR REPLACE TRIGGER forge_observation_attempt_delete BEFORE DELETE ON "
            "forge_learning_observation_attempts FOR EACH ROW EXECUTE FUNCTION forge_refuse_immutable_change()"
        )
    )
