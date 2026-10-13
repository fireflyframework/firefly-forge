# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native relational identities, single-use OIDC attempts and revocable sessions."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import DateTime, Float, ForeignKey, String, Text, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeIdentityUserEntity(BaseEntity):
    __tablename__ = "forge_identity_users"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)
    issuer: Mapped[str] = mapped_column(String(2048))
    subject: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    contact_email: Mapped[str | None] = mapped_column(String(320))
    enabled: Mapped[bool] = mapped_column(default=True)
    api_roles: Mapped[list[str]] = mapped_column(JSONB, default=list)
    api_roles_audience: Mapped[str | None] = mapped_column(String(2048))
    api_roles_issued_at: Mapped[float | None] = mapped_column(Float)
    api_roles_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ForgeIdentitySessionEntity(BaseEntity):
    __tablename__ = "forge_identity_sessions"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("forge_identity_users.id"), index=True)
    cookie_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="active")
    revision: Mapped[int] = mapped_column(default=1)
    encrypted_data: Mapped[str] = mapped_column(Text)


class ForgeIdentityAttemptEntity(BaseEntity):
    __tablename__ = "forge_identity_attempts"
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    browser_nonce_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed: Mapped[bool] = mapped_column(default=False)
    encrypted_data: Mapped[str] = mapped_column(Text)


@repository
class ForgeIdentityUserRepository(Repository[ForgeIdentityUserEntity, UUID]):
    async def subject(self, issuer: str, subject: str) -> ForgeIdentityUserEntity | None:
        return await self._require_session().scalar(
            select(ForgeIdentityUserEntity)
            .where(ForgeIdentityUserEntity.issuer == issuer, ForgeIdentityUserEntity.subject == subject)
            .with_for_update()
        )


@repository
class ForgeIdentitySessionRepository(Repository[ForgeIdentitySessionEntity, UUID]):
    async def cookie(self, digest: str) -> ForgeIdentitySessionEntity | None:
        return await self._require_session().scalar(
            select(ForgeIdentitySessionEntity).where(ForgeIdentitySessionEntity.cookie_hash == digest).with_for_update()
        )


@repository
class ForgeIdentityAttemptRepository(Repository[ForgeIdentityAttemptEntity, UUID]):
    async def state(self, digest: str) -> ForgeIdentityAttemptEntity | None:
        return await self._require_session().scalar(
            select(ForgeIdentityAttemptEntity).where(ForgeIdentityAttemptEntity.state_hash == digest).with_for_update()
        )
