# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native policy admission; consent never enables an unreviewed agent adapter."""

from dataclasses import asdict
from typing import Any
from uuid import uuid4

from pyfly.container import service
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from firefly_forge.connection_oauth_models import OAuthPolicyBinding, OAuthProvider
from firefly_forge.identity import Principal
from firefly_forge.journal import digest
from firefly_forge.oauth_authority import require_oauth_administrator
from firefly_forge.oauth_provider_policy_models import LOCAL_PROVIDER, OAuthProviderPolicyInput, local_origin
from firefly_forge.oauth_provider_policy_persistence import (
    OAuthProviderPolicyEntity,
    OAuthProviderPolicyOperationEntity,
    OAuthProviderPolicyRepository,
    OAuthProviderPolicyRevisionEntity,
)
from firefly_forge.store import Store

Data = dict[str, Any]


@service
class OAuthProviderPolicies:
    def __init__(
        self, store: Store, records: OAuthProviderPolicyRepository, *, local_development_available: bool = False
    ) -> None:
        self.store, self.records = store, records
        self.local_development_available = local_development_available is True

    @staticmethod
    def _document(origin: str, enabled: bool) -> Data:
        origin = local_origin(origin)
        policy = OAuthProvider(
            id=LOCAL_PROVIDER,
            issuer=origin,
            authorization_endpoint=origin + "/oauth2/authorize",
            token_endpoint=origin + "/oauth2/token",
            resource=origin + "/mcp/readonly",
            scopes=("repository:read",),
        )
        return {
            "mode": "local_development",
            "enabled": enabled,
            "local_origin": origin,
            "provider": {**asdict(policy), "scopes": list(policy.scopes)},
        }

    async def _revision(
        self, session: AsyncSession, row: OAuthProviderPolicyEntity
    ) -> OAuthProviderPolicyRevisionEntity:
        revision = await session.scalar(
            select(OAuthProviderPolicyRevisionEntity).where(
                OAuthProviderPolicyRevisionEntity.policy_id == row.id,
                OAuthProviderPolicyRevisionEntity.revision == row.revision,
            )
        )
        latest = await session.scalar(
            select(func.max(OAuthProviderPolicyRevisionEntity.revision)).where(
                OAuthProviderPolicyRevisionEntity.policy_id == row.id
            )
        )
        if (
            revision is None
            or latest != row.revision
            or revision.digest != row.digest
            or digest(revision.data) != row.digest
        ):
            raise ValueError("Current provider policy history is unavailable or changed")
        data = revision.data
        if (
            row.name != LOCAL_PROVIDER
            or type(data.get("enabled")) is not bool
            or not isinstance(data.get("local_origin"), str)
            or data != self._document(data["local_origin"], data["enabled"])
        ):
            raise ValueError("Provider policy does not match its admitted development contract")
        return revision

    @staticmethod
    def _view(row: OAuthProviderPolicyEntity, revision: OAuthProviderPolicyRevisionEntity) -> Data:
        return {
            "name": row.name,
            "revision": row.revision,
            "digest": row.digest,
            **revision.data,
        }

    async def lookup(self, session: AsyncSession, name: str) -> OAuthPolicyBinding | None:
        if name != LOCAL_PROVIDER or not self.local_development_available:
            return None
        row = await session.scalar(
            select(OAuthProviderPolicyEntity).where(OAuthProviderPolicyEntity.name == name).with_for_update(read=True)
        )
        if row is None:
            return None
        revision = await self._revision(session, row)
        if revision.data["enabled"] is not True:
            return None
        data = revision.data["provider"]
        return OAuthPolicyBinding(OAuthProvider(**{**data, "scopes": tuple(data["scopes"])}), row.revision, row.digest)

    async def directory(self, actor: Principal) -> Data:
        async with self.store._unit() as session:
            await require_oauth_administrator(session, actor)
            row = await session.scalar(
                select(OAuthProviderPolicyEntity)
                .where(OAuthProviderPolicyEntity.name == LOCAL_PROVIDER)
                .with_for_update(read=True)
            )
            result = {
                "local_development_available": self.local_development_available,
                "policies": [self._view(row, await self._revision(session, row))] if row else [],
            }
            await self.store.admit_credentials(result)
            return result

    async def configure(self, name: str, request: OAuthProviderPolicyInput, actor: Principal) -> Data:
        if name != LOCAL_PROVIDER:
            raise ValueError("Select an admitted provider policy")
        async with self.store._unit() as session:
            await require_oauth_administrator(session, actor)
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": self.store.schema + ":oauth-provider-policy:" + name},
            )
            row = await self.records.named(name)
            if request.enabled and not self.local_development_available:
                raise ValueError("Enable the explicit local development configuration before admitting this service")
            if row is None and not self.local_development_available:
                raise ValueError("Local development policy is unavailable")
            if row:
                await self._revision(session, row)
                previous = await session.scalar(
                    select(OAuthProviderPolicyOperationEntity).where(
                        OAuthProviderPolicyOperationEntity.policy_id == row.id,
                        OAuthProviderPolicyOperationEntity.request_id == request.request_id,
                    )
                )
                if previous:
                    if (
                        previous.data["request_digest"] != digest(request.model_dump())
                        or previous.data["actor_id"] != actor.id.hex
                    ):
                        raise ValueError("Policy request identifier belongs to another admission")
                    result = previous.data["result"]
                    await self.store.admit_credentials(result)
                    return dict(result)
            current = row.revision if row else 0
            if current != request.expected_revision:
                raise ValueError("Provider policy changed; refresh its current configuration")
            document = self._document(request.local_origin, request.enabled)
            revision_number = current + 1
            packet = {"name": name, "revision": revision_number, "digest": digest(document), **document}
            audit = {
                "actor_id": actor.id.hex,
                "request_digest": digest(request.model_dump()),
                "result": packet,
                "previous_revision": current,
            }
            await self.store.admit_credentials({"request": request.model_dump(), "result": packet, "audit": audit})
            if row is None:
                row = OAuthProviderPolicyEntity(
                    id=uuid4(), name=name, revision=revision_number, digest=packet["digest"]
                )
                await self.records.save(row)
            else:
                row.revision, row.digest = revision_number, packet["digest"]
            session.add(
                OAuthProviderPolicyRevisionEntity(
                    id=uuid4(), policy_id=row.id, revision=revision_number, digest=row.digest, data=document
                )
            )
            session.add(
                OAuthProviderPolicyOperationEntity(
                    id=uuid4(), policy_id=row.id, request_id=request.request_id, data=audit
                )
            )
            await session.flush()
            return packet
