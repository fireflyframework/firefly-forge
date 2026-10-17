# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Immutable original run admission receipts, distinct from mutable run state."""

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import CheckConstraint, ForeignKey, String, Text, cast, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeRunCreationRequestEntity(BaseEntity):
    __tablename__ = "forge_run_creation_requests"
    __table_args__ = (CheckConstraint("octet_length(data::text) <= 262144", name="forge_run_creation_envelope_bound"),)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("forge_identity_users.id"), index=True)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("forge_repositories.id"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("forge_projects.id"), nullable=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("forge_runs.id"), unique=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    receipt_digest: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeRunCreationRequestRepository(Repository[ForgeRunCreationRequestEntity, UUID]):
    async def envelope_bytes(self, request_id: UUID) -> int | None:
        result = await self._require_session().scalar(
            select(func.octet_length(cast(ForgeRunCreationRequestEntity.data, Text))).where(
                ForgeRunCreationRequestEntity.id == request_id
            )
        )
        return int(result) if result is not None else None


class ForgeRunCreationReservationEntity(BaseEntity):
    __tablename__ = "forge_run_creation_reservations"
    __table_args__ = (
        CheckConstraint("octet_length(data::text) <= 4096", name="forge_run_creation_reservation_bound"),
        CheckConstraint("(project_id IS NULL) = (component_id IS NULL)", name="forge_run_creation_reservation_scope"),
    )
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("forge_identity_users.id"), index=True)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("forge_repositories.id"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("forge_projects.id"), nullable=True)
    component_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authentication_method: Mapped[str] = mapped_column(String(32))
    request_digest: Mapped[str] = mapped_column(String(64))
    receipt_digest: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeRunCreationCancellationEntity(BaseEntity):
    __tablename__ = "forge_run_creation_cancellations"
    __table_args__ = (
        CheckConstraint("octet_length(data::text) <= 4096", name="forge_run_creation_cancellation_bound"),
        CheckConstraint("(project_id IS NULL) = (component_id IS NULL)", name="forge_run_creation_cancellation_scope"),
    )
    id: Mapped[UUID] = mapped_column(ForeignKey("forge_run_creation_reservations.id"), primary_key=True)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("forge_identity_users.id"), index=True)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("forge_repositories.id"))
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("forge_projects.id"), nullable=True)
    component_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authentication_method: Mapped[str] = mapped_column(String(32))
    cancelled_authentication_method: Mapped[str] = mapped_column(String(32))
    request_digest: Mapped[str] = mapped_column(String(64))
    receipt_digest: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeRunCreationReservationRepository(Repository[ForgeRunCreationReservationEntity, UUID]):
    async def envelope_bytes(self, request_id: UUID) -> int | None:
        result = await self._require_session().scalar(
            select(func.octet_length(cast(ForgeRunCreationReservationEntity.data, Text))).where(
                ForgeRunCreationReservationEntity.id == request_id
            )
        )
        return int(result) if result is not None else None

    async def pending_for_actor(self, actor_id: UUID) -> int:
        reservation = ForgeRunCreationReservationEntity
        result = await self._require_session().scalar(
            select(func.count())
            .select_from(reservation)
            .where(
                reservation.actor_id == actor_id,
                ~select(ForgeRunCreationRequestEntity.id)
                .where(ForgeRunCreationRequestEntity.id == reservation.id)
                .exists(),
                ~select(ForgeRunCreationCancellationEntity.id)
                .where(ForgeRunCreationCancellationEntity.id == reservation.id)
                .exists(),
            )
        )
        return int(result or 0)


@repository
class ForgeRunCreationCancellationRepository(Repository[ForgeRunCreationCancellationEntity, UUID]):
    async def envelope_bytes(self, request_id: UUID) -> int | None:
        result = await self._require_session().scalar(
            select(func.octet_length(cast(ForgeRunCreationCancellationEntity.data, Text))).where(
                ForgeRunCreationCancellationEntity.id == request_id
            )
        )
        return int(result) if result is not None else None
