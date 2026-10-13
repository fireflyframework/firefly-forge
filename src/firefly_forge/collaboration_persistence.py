# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native PyFly repositories for trusted repository collaboration and immutable briefs."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import BigInteger, ForeignKey, Identity, String, UniqueConstraint, and_, case, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class ForgeCollaborationSettingsEntity(BaseEntity):
    __tablename__ = "forge_collaboration_settings"
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("forge_repositories.id"), unique=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeWebhookInboxEntity(BaseEntity):
    __tablename__ = "forge_webhook_inbox"
    __table_args__ = (
        UniqueConstraint("repository_id", "provider", "delivery_id"),
        UniqueConstraint("repository_id", "provider", "source_key"),
    )
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("forge_repositories.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    delivery_id: Mapped[str] = mapped_column(String(200))
    source_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeProductDefinitionEntity(BaseEntity):
    __tablename__ = "forge_product_definitions"
    __table_args__ = (UniqueConstraint("repository_id", "revision"),)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("forge_repositories.id"), index=True)
    revision: Mapped[int] = mapped_column()
    digest: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ForgeChannelInboxEntity(BaseEntity):
    __tablename__ = "forge_channel_inbox"
    __table_args__ = (UniqueConstraint("provider", "delivery_key"),)
    provider: Mapped[str] = mapped_column(String(32))
    delivery_key: Mapped[str] = mapped_column(String(200))
    digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class ForgeChannelInboxRepository(Repository[ForgeChannelInboxEntity, UUID]):
    async def find_delivery(self, provider: str, delivery_key: str) -> ForgeChannelInboxEntity | None:
        return await self._require_session().scalar(
            select(ForgeChannelInboxEntity).where(
                ForgeChannelInboxEntity.provider == provider, ForgeChannelInboxEntity.delivery_key == delivery_key
            )
        )

    async def latest(self, provider: str) -> list[ForgeChannelInboxEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeChannelInboxEntity)
                .where(ForgeChannelInboxEntity.provider == provider)
                .order_by(ForgeChannelInboxEntity.created_at.desc())
                .limit(100)
            )
        )


@repository
class ForgeCollaborationSettingsRepository(Repository[ForgeCollaborationSettingsEntity, UUID]):
    async def find_for_repository(self, repository_id: UUID) -> ForgeCollaborationSettingsEntity | None:
        return await self._require_session().scalar(
            select(ForgeCollaborationSettingsEntity).where(
                ForgeCollaborationSettingsEntity.repository_id == repository_id
            )
        )


@repository
class ForgeWebhookInboxRepository(Repository[ForgeWebhookInboxEntity, UUID]):
    async def find_delivery(
        self, repository_id: UUID, provider: str, delivery_id: str
    ) -> ForgeWebhookInboxEntity | None:
        return await self._require_session().scalar(
            select(ForgeWebhookInboxEntity).where(
                ForgeWebhookInboxEntity.repository_id == repository_id,
                ForgeWebhookInboxEntity.provider == provider,
                ForgeWebhookInboxEntity.delivery_id == delivery_id,
            )
        )

    async def find_source(self, repository_id: UUID, provider: str, source_key: str) -> ForgeWebhookInboxEntity | None:
        return await self._require_session().scalar(
            select(ForgeWebhookInboxEntity).where(
                ForgeWebhookInboxEntity.repository_id == repository_id,
                ForgeWebhookInboxEntity.provider == provider,
                ForgeWebhookInboxEntity.source_key == source_key,
            )
        )

    async def find_latest(self, repository_id: UUID | None = None) -> list[ForgeWebhookInboxEntity]:
        statement = select(ForgeWebhookInboxEntity).order_by(ForgeWebhookInboxEntity.sequence.desc()).limit(200)
        if repository_id is not None:
            statement = statement.where(ForgeWebhookInboxEntity.repository_id == repository_id)
        return list(await self._require_session().scalars(statement))

    async def find_pending(self) -> list[ForgeWebhookInboxEntity]:
        return list(
            await self._require_session().scalars(
                select(ForgeWebhookInboxEntity)
                .where(
                    or_(
                        ForgeWebhookInboxEntity.status == "received",
                        and_(
                            ForgeWebhookInboxEntity.status == "needs_reconciliation",
                            ForgeWebhookInboxEntity.data["feedback_status"].as_string() == "writing",
                        ),
                        and_(
                            ForgeWebhookInboxEntity.status == "processed",
                            or_(
                                ForgeWebhookInboxEntity.data["feedback_status"].as_string().in_(["pending", "writing"]),
                                and_(
                                    ForgeWebhookInboxEntity.data["feedback_status"].as_string() == "sent",
                                    ForgeWebhookInboxEntity.data["feedback_stage"]
                                    .as_string()
                                    .not_in(["succeeded", "failed", "cancelled", "interrupted", "action_completed"]),
                                ),
                            ),
                        ),
                    )
                )
                .order_by(
                    case(
                        (ForgeWebhookInboxEntity.status == "received", 0),
                        (ForgeWebhookInboxEntity.data["feedback_status"].as_string().in_(["pending", "writing"]), 1),
                        else_=2,
                    ),
                    ForgeWebhookInboxEntity.sequence,
                )
                .limit(200)
            )
        )


@repository
class ForgeProductDefinitionRepository(Repository[ForgeProductDefinitionEntity, UUID]):
    async def find_revision(self, repository_id: UUID, revision: int) -> ForgeProductDefinitionEntity | None:
        return await self._require_session().scalar(
            select(ForgeProductDefinitionEntity).where(
                ForgeProductDefinitionEntity.repository_id == repository_id,
                ForgeProductDefinitionEntity.revision == revision,
            )
        )

    async def find_change(self, repository_id: UUID, request_id: str) -> ForgeProductDefinitionEntity | None:
        return await self._require_session().scalar(
            select(ForgeProductDefinitionEntity)
            .where(
                ForgeProductDefinitionEntity.repository_id == repository_id,
                ForgeProductDefinitionEntity.data["change_receipt"]["request_id"].as_string() == request_id,
            )
            .limit(1)
        )

    async def find_current(self, repository_id: UUID) -> ForgeProductDefinitionEntity | None:
        return await self._require_session().scalar(
            select(ForgeProductDefinitionEntity)
            .where(ForgeProductDefinitionEntity.repository_id == repository_id)
            .order_by(ForgeProductDefinitionEntity.revision.desc())
            .limit(1)
        )
