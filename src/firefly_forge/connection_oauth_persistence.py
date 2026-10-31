# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Connection revisions, encrypted grant families and single-dispatch OAuth receipts."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class OAuthConnectionEntity(BaseEntity):
    __tablename__ = "forge_oauth_connections"
    name: Mapped[str] = mapped_column(String(64), unique=True)
    revision: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(32))


class OAuthRegistrationEntity(BaseEntity):
    __tablename__ = "forge_oauth_registrations"
    __table_args__ = (UniqueConstraint("connection_id", "revision"),)
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("forge_oauth_connections.id"), index=True)
    revision: Mapped[int] = mapped_column()
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    encrypted_secret: Mapped[str] = mapped_column(Text)


class OAuthAttemptEntity(BaseEntity):
    __tablename__ = "forge_oauth_attempts"
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("forge_oauth_connections.id"), index=True)
    revision: Mapped[int] = mapped_column()
    actor_id: Mapped[UUID] = mapped_column()
    kind: Mapped[str] = mapped_column(String(16))
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    nonce_digest: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed: Mapped[bool] = mapped_column(default=False)
    encrypted_data: Mapped[str] = mapped_column(Text)


class OAuthGrantEntity(BaseEntity):
    __tablename__ = "forge_oauth_grants"
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("forge_oauth_connections.id"), index=True)
    registration_revision: Mapped[int] = mapped_column()
    family: Mapped[str] = mapped_column(String(32), unique=True)
    revision: Mapped[int] = mapped_column(default=1)
    actor_id: Mapped[UUID] = mapped_column()
    status: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    encrypted_data: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class OAuthEffectEntity(BaseEntity):
    __tablename__ = "forge_oauth_effects"
    __table_args__ = (UniqueConstraint("connection_id", "request_id"),)
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("forge_oauth_connections.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(32))
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class OAuthEventEntity(BaseEntity):
    __tablename__ = "forge_oauth_events"
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("forge_oauth_connections.id"), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class OAuthCredentialEvidenceEntity(BaseEntity):
    __tablename__ = "forge_oauth_credential_evidence"
    __table_args__ = (UniqueConstraint("kind", "source_id", "revision"),)
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("forge_oauth_connections.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[UUID] = mapped_column()
    revision: Mapped[int] = mapped_column()
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class OAuthConnectionRepository(Repository[OAuthConnectionEntity, UUID]):
    async def named(self, name: str) -> OAuthConnectionEntity | None:
        return await self._require_session().scalar(
            select(OAuthConnectionEntity).where(OAuthConnectionEntity.name == name).with_for_update()
        )
