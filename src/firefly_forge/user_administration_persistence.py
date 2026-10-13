# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native administration intents and immutable identity-change evidence."""

from typing import Any, cast
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Mapped, mapped_column

from firefly_forge.identity_persistence import ForgeIdentitySessionEntity


class ForgeIdentityAdminOperationEntity(BaseEntity):
    __tablename__ = "forge_identity_admin_operations"
    __table_args__ = (UniqueConstraint("request_id"),)
    request_id: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(32), index=True)
    encrypted_config: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeIdentityAdminEventEntity(BaseEntity):
    __tablename__ = "forge_identity_admin_events"
    operation_id: Mapped[UUID | None] = mapped_column(ForeignKey("forge_identity_admin_operations.id"), index=True)
    actor_id: Mapped[UUID] = mapped_column()
    target_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("forge_identity_users.id"), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeIdentityAdminOperationRepository(Repository[ForgeIdentityAdminOperationEntity, UUID]):
    async def unfinished(self) -> list[ForgeIdentityAdminOperationEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeIdentityAdminOperationEntity)
                .where(ForgeIdentityAdminOperationEntity.status == "executing")
                .order_by(ForgeIdentityAdminOperationEntity.created_at)
                .limit(500)
                .with_for_update()
            )
        )

    async def request(self, request_id: str) -> ForgeIdentityAdminOperationEntity | None:
        return await self._require_session().scalar(
            select(ForgeIdentityAdminOperationEntity)
            .where(ForgeIdentityAdminOperationEntity.request_id == request_id)
            .with_for_update()
        )

    async def latest(self) -> list[ForgeIdentityAdminOperationEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeIdentityAdminOperationEntity)
                .order_by(ForgeIdentityAdminOperationEntity.created_at.desc())
                .limit(100)
            )
        )


@repository
class ForgeIdentityAdminEventRepository(Repository[ForgeIdentityAdminEventEntity, UUID]):
    async def for_operation(self, operation_id: UUID) -> list[ForgeIdentityAdminEventEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeIdentityAdminEventEntity)
                .where(ForgeIdentityAdminEventEntity.operation_id == operation_id)
                .order_by(ForgeIdentityAdminEventEntity.data["sequence"].as_integer())
            )
        )


@repository
class ForgeAdminSessionRepository(Repository[ForgeIdentitySessionEntity, UUID]):
    async def revoke_for_user(self, user_id: UUID, encrypted_empty: str) -> int:
        result = await self._require_session().execute(
            update(ForgeIdentitySessionEntity)
            .where(
                ForgeIdentitySessionEntity.user_id == user_id,
                ForgeIdentitySessionEntity.status != "revoked",
            )
            .values(status="revoked", revision=ForgeIdentitySessionEntity.revision + 1, encrypted_data=encrypted_empty)
        )
        return cast(CursorResult[Any], result).rowcount
