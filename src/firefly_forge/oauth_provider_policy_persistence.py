# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Shared policy pointers and immutable admission history."""

from typing import Any
from uuid import UUID

from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import BaseEntity, Repository
from sqlalchemy import ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


class OAuthProviderPolicyEntity(BaseEntity):
    __tablename__ = "forge_oauth_provider_policies"
    name: Mapped[str] = mapped_column(String(64), unique=True)
    revision: Mapped[int] = mapped_column()
    digest: Mapped[str] = mapped_column(String(64))


class OAuthProviderPolicyRevisionEntity(BaseEntity):
    __tablename__ = "forge_oauth_provider_policy_revisions"
    __table_args__ = (UniqueConstraint("policy_id", "revision"),)
    policy_id: Mapped[UUID] = mapped_column(ForeignKey("forge_oauth_provider_policies.id"), index=True)
    revision: Mapped[int] = mapped_column()
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    digest: Mapped[str] = mapped_column(String(64))


class OAuthProviderPolicyOperationEntity(BaseEntity):
    __tablename__ = "forge_oauth_provider_policy_operations"
    __table_args__ = (UniqueConstraint("policy_id", "request_id"),)
    policy_id: Mapped[UUID] = mapped_column(ForeignKey("forge_oauth_provider_policies.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)


@repository
class OAuthProviderPolicyRepository(Repository[OAuthProviderPolicyEntity, UUID]):
    async def named(self, name: str) -> OAuthProviderPolicyEntity | None:
        return await self._require_session().scalar(
            select(OAuthProviderPolicyEntity).where(OAuthProviderPolicyEntity.name == name).with_for_update()
        )
