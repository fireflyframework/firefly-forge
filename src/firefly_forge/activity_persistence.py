# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Private event payloads kept outside run and evidence JSON projections."""

from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import CheckConstraint, ForeignKey, LargeBinary, String, func, select
from sqlalchemy.orm import Mapped, mapped_column

from firefly_forge.activity_models import MAX_EVENT_BYTES, ActivityUnavailable


class ForgeEventContentEntity(BaseEntity):
    __tablename__ = "forge_event_content"
    __table_args__ = (
        CheckConstraint("version = 1"),
        CheckConstraint("byte_count >= 0 AND byte_count <= 4194304"),
        CheckConstraint("octet_length(raw_bytes) = byte_count"),
        CheckConstraint("sha256 ~ '^[a-f0-9]{64}$'"),
    )
    event_id: Mapped[UUID] = mapped_column(ForeignKey("forge_events.id"), unique=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("forge_runs.id"), index=True)
    version: Mapped[int] = mapped_column()
    byte_count: Mapped[int] = mapped_column()
    sha256: Mapped[str] = mapped_column(String(64))
    raw_bytes: Mapped[bytes] = mapped_column(LargeBinary)


@repository
class ForgeEventContentRepository(Repository[ForgeEventContentEntity, UUID]):
    async def budget(self, run_id: UUID) -> tuple[int, int]:
        row = (
            await self._require_session().execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(func.octet_length(ForgeEventContentEntity.raw_bytes)), 0),
                ).where(ForgeEventContentEntity.run_id == run_id)
            )
        ).one()
        return int(row[0]), int(row[1])

    async def payload(self, run_id: UUID, event_id: UUID, size: int, sha256: str) -> bytes:
        if type(size) is not int or not 0 <= size <= MAX_EVENT_BYTES:
            raise ActivityUnavailable("activity_content_limit")
        entity = ForgeEventContentEntity
        predicates = (entity.run_id == run_id, entity.event_id == event_id)
        session = self._require_session()
        header = (
            await session.execute(
                select(entity.version, entity.byte_count, entity.sha256, func.octet_length(entity.raw_bytes)).where(
                    *predicates
                )
            )
        ).one_or_none()
        if header is None:
            raise ActivityUnavailable("activity_content_missing")
        if tuple(header) != (1, size, sha256, size):
            raise ActivityUnavailable("activity_integrity")
        payload = await session.scalar(
            select(entity.raw_bytes).where(
                *predicates,
                entity.version == 1,
                entity.byte_count == size,
                entity.sha256 == sha256,
                func.octet_length(entity.raw_bytes) == size,
            )
        )
        if not isinstance(payload, bytes) or len(payload) != size:
            raise ActivityUnavailable("activity_integrity")
        return payload
