# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native relational credential custody; plaintext secrets have no storage field."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository, UtcDateTime
from sqlalchemy import ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgePersonalTokenEntity(BaseEntity):
    __tablename__ = "forge_personal_tokens"
    __table_args__ = (UniqueConstraint("owner_id", "request_id"),)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("forge_identity_users.id"), index=True)
    owner_identity_digest: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(32))
    request_digest: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(160))
    secret_digest: Mapped[str] = mapped_column(String(64), unique=True)
    project_ids: Mapped[list[str]] = mapped_column(JSONB)
    permissions: Mapped[list[str]] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    predecessor_id: Mapped[UUID | None] = mapped_column(ForeignKey("forge_personal_tokens.id"), nullable=True)


class ForgePersonalTokenEventEntity(BaseEntity):
    __tablename__ = "forge_personal_token_events"
    token_id: Mapped[UUID] = mapped_column(ForeignKey("forge_personal_tokens.id"), index=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("forge_identity_users.id"), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgePersonalTokenRepository(Repository[ForgePersonalTokenEntity, UUID]):
    async def request(self, owner_id: UUID, request_id: str) -> ForgePersonalTokenEntity | None:
        return await self._require_session().scalar(
            select(ForgePersonalTokenEntity).where(
                ForgePersonalTokenEntity.owner_id == owner_id, ForgePersonalTokenEntity.request_id == request_id
            )
        )

    async def page(
        self, owner_id: UUID, after: UUID | None, limit: int, *, identity_digest: str
    ) -> list[ForgePersonalTokenEntity]:
        query = select(ForgePersonalTokenEntity).where(
            ForgePersonalTokenEntity.owner_id == owner_id,
            ForgePersonalTokenEntity.owner_identity_digest == identity_digest,
        )
        if after is not None:
            query = query.where(ForgePersonalTokenEntity.id > after)
        return list(await self._require_session().scalars(query.order_by(ForgePersonalTokenEntity.id).limit(limit)))


@repository
class ForgePersonalTokenEventRepository(Repository[ForgePersonalTokenEventEntity, UUID]):
    pass
