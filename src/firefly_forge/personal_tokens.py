# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One-time personal observation credentials with current native project grants."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import SecretStr
from pyfly.container import service
from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.security.authentication import Authentication, BadCredentialsException

from firefly_forge.access_persistence import ForgeMembershipRepository
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.personal_token_format import PERSONAL_TOKEN_PATTERN, PERSONAL_TOKEN_PREFIX
from firefly_forge.personal_token_models import PersonalTokenInput, PersonalTokenRotationInput
from firefly_forge.personal_token_persistence import (
    ForgePersonalTokenEntity,
    ForgePersonalTokenEventEntity,
    ForgePersonalTokenEventRepository,
    ForgePersonalTokenRepository,
)
from firefly_forge.product_provenance import content_digest
from firefly_forge.project_access import ROLE_PERMISSIONS, AccessDenied
from firefly_forge.store import Store, identifier


@dataclass(frozen=True)
class PersonalTokenDisclosure:
    credential: dict[str, Any]
    secret: SecretStr | None = None

    def disclose(self) -> dict[str, Any]:
        return {
            "credential": self.credential,
            "secret_state": "returned_once" if self.secret is not None else "not_retained",
            **({"token": self.secret.get_secret_value()} if self.secret is not None else {}),
        }


@dataclass(frozen=True)
class PersonalTokenGrant:
    principal: Principal
    memberships: list[dict[str, Any]]


@service
class PersonalTokens:
    def __init__(
        self,
        store: Store,
        records: ForgePersonalTokenRepository | None = None,
        events: ForgePersonalTokenEventRepository | None = None,
        users: ForgeIdentityUserRepository | None = None,
        memberships: ForgeMembershipRepository | None = None,
    ) -> None:
        self.store = store
        self.records = records or ForgePersonalTokenRepository()
        self.events = events or ForgePersonalTokenEventRepository()
        self.users = users or ForgeIdentityUserRepository()
        self.memberships = memberships or ForgeMembershipRepository()

    @staticmethod
    def owner_identity(issuer: str, subject: str) -> str:
        return content_digest({"issuer": issuer, "subject": subject})

    @staticmethod
    def metadata(row: ForgePersonalTokenEntity) -> dict[str, Any]:
        return {
            "id": row.id.hex,
            "name": row.name,
            "prefix": PERSONAL_TOKEN_PREFIX + row.id.hex[:8],
            "project_ids": list(row.project_ids),
            "permissions": list(row.permissions),
            "created_at": row.created_at.isoformat(),
            "expires_at": row.expires_at.isoformat(),
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at is not None else None,
            "status": "revoked"
            if row.revoked_at is not None
            else "expired"
            if row.expires_at <= datetime.now(UTC)
            else "active",
            "predecessor_id": row.predecessor_id.hex if row.predecessor_id else None,
        }

    async def _owner(self, actor: Principal) -> None:
        if actor.authentication_method != "oidc_session" or actor.kind != "human":
            raise AccessDenied("Manage personal credentials through your signed-in user session")
        row = await self.users.find_by_id(actor.id, lock=LockMode.PESSIMISTIC_WRITE)
        if row is None or not row.enabled or (row.issuer, row.subject) != (actor.issuer, actor.subject):
            raise AccessDenied("The credential owner is unavailable or suspended")

    async def _projects(self, actor: Principal, projects: list[str]) -> None:
        allowed = {
            row.project_id.hex
            for row in await self.memberships.for_user(actor.id)
            if row.active and "read" in ROLE_PERMISSIONS.get(row.role, frozenset())
        }
        if not projects or not set(projects) <= allowed:
            raise AccessDenied("Choose only projects where you have a current membership")

    @staticmethod
    def _future(expiry: datetime) -> None:
        if expiry.tzinfo is None or expiry <= datetime.now(UTC):
            raise ValueError("Choose a future expiry with an explicit timezone")

    async def _event(self, row: ForgePersonalTokenEntity, kind: str) -> None:
        await self.events.save(
            ForgePersonalTokenEventEntity(
                id=uuid4(),
                token_id=row.id,
                owner_id=row.owner_id,
                data={"kind": kind, "project_ids": list(row.project_ids), "at": datetime.now(UTC).isoformat()},
            )
        )

    async def _issue(
        self,
        actor: Principal,
        request: PersonalTokenInput,
        request_digest: str,
        predecessor: ForgePersonalTokenEntity | None = None,
    ) -> PersonalTokenDisclosure:
        self._future(request.expires_at)
        if request.permissions != ["read"]:
            raise ValueError("Personal credentials support project observation only")
        await self._projects(actor, request.project_ids)
        self.store.reject_credentials(request.model_dump(mode="json"))
        token_id, now = uuid4(), datetime.now(UTC)
        raw = PERSONAL_TOKEN_PREFIX + token_id.hex + "." + secrets.token_urlsafe(32)
        row = ForgePersonalTokenEntity(
            id=token_id,
            owner_id=actor.id,
            owner_identity_digest=self.owner_identity(actor.issuer, actor.subject),
            request_id=request.request_id,
            request_digest=request_digest,
            name=request.name,
            secret_digest=hashlib.sha256(raw.encode()).hexdigest(),
            project_ids=list(request.project_ids),
            permissions=["read"],
            created_at=now,
            expires_at=request.expires_at.astimezone(UTC),
            revoked_at=None,
            predecessor_id=predecessor.id if predecessor else None,
        )
        await self.records.save(row)
        if predecessor is not None:
            predecessor.revoked_at = now
            await self.records.save(predecessor)
            await self._event(predecessor, "rotated")
        await self._event(row, "created")
        return PersonalTokenDisclosure(self.metadata(row), SecretStr(raw))

    async def create(self, actor: Principal, request: PersonalTokenInput) -> PersonalTokenDisclosure:
        request_digest = content_digest({"operation": "create", **request.model_dump(mode="json")})
        async with self.store._unit():
            await self._owner(actor)
            existing = await self.records.request(actor.id, request.request_id)
            if existing is not None:
                if existing.owner_identity_digest != self.owner_identity(actor.issuer, actor.subject):
                    raise AccessDenied("This credential belongs to a different identity")
                if existing.request_digest != request_digest:
                    raise ValueError("This credential request already identifies different input")
                return PersonalTokenDisclosure(self.metadata(existing))
            return await self._issue(actor, request, request_digest)

    async def page(self, actor: Principal, *, after: str | None = None, limit: int = 25) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("Choose a page size between 1 and 200")
        cursor = identifier(after) if after is not None else None
        async with self.store._unit():
            await self._owner(actor)
            rows = await self.records.page(
                actor.id, cursor, limit + 1, identity_digest=self.owner_identity(actor.issuer, actor.subject)
            )
            items = [self.metadata(row) for row in rows[:limit]]
            return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}

    async def revoke(self, actor: Principal, token_id: str) -> dict[str, Any]:
        async with self.store._unit():
            await self._owner(actor)
            row = await self.records.find_by_id(identifier(token_id), lock=LockMode.PESSIMISTIC_WRITE)
            if (
                row is None
                or row.owner_id != actor.id
                or row.owner_identity_digest != self.owner_identity(actor.issuer, actor.subject)
            ):
                raise KeyError("Personal credential not found")
            if row.revoked_at is None:
                row.revoked_at = datetime.now(UTC)
                await self.records.save(row)
                await self._event(row, "revoked")
            return self.metadata(row)

    async def rotate(
        self, actor: Principal, token_id: str, request: PersonalTokenRotationInput
    ) -> PersonalTokenDisclosure:
        predecessor_id = identifier(token_id)
        request_digest = content_digest(
            {"operation": "rotate", "predecessor_id": predecessor_id.hex, **request.model_dump(mode="json")}
        )
        async with self.store._unit():
            await self._owner(actor)
            existing = await self.records.request(actor.id, request.request_id)
            if existing is not None:
                if existing.owner_identity_digest != self.owner_identity(actor.issuer, actor.subject):
                    raise AccessDenied("This credential belongs to a different identity")
                if existing.request_digest != request_digest:
                    raise ValueError("This credential request already identifies different input")
                return PersonalTokenDisclosure(self.metadata(existing))
            predecessor = await self.records.find_by_id(predecessor_id, lock=LockMode.PESSIMISTIC_WRITE)
            if (
                predecessor is None
                or predecessor.owner_id != actor.id
                or predecessor.owner_identity_digest != self.owner_identity(actor.issuer, actor.subject)
            ):
                raise KeyError("Personal credential not found")
            if predecessor.revoked_at is not None:
                raise ValueError("This credential has already been revoked or rotated")
            values = PersonalTokenInput(
                request_id=request.request_id,
                name=predecessor.name,
                project_ids=list(predecessor.project_ids),
                expires_at=request.expires_at,
            )
            return await self._issue(actor, values, request_digest, predecessor)

    async def _grant(self, token_id: UUID, owner_id: UUID, digest: str | None = None) -> PersonalTokenGrant:
        # Lifecycle changes lock owner then credential; authentication uses the same order.
        user = await self.users.find_by_id(owner_id, lock=LockMode.PESSIMISTIC_READ)
        row = await self.records.find_by_id(token_id, lock=LockMode.PESSIMISTIC_READ)
        valid_digest = digest is None or hmac.compare_digest(digest, row.secret_digest if row else "0" * 64)
        if (
            row is None
            or row.owner_id != owner_id
            or not valid_digest
            or row.permissions != ["read"]
            or row.revoked_at is not None
            or row.expires_at <= datetime.now(UTC)
            or user is None
            or not user.enabled
            or row.owner_identity_digest != self.owner_identity(user.issuer, user.subject)
        ):
            raise AccessDenied("This personal credential is unavailable")
        memberships = [
            {
                "project_id": membership.project_id.hex,
                "role": "viewer",
                "permissions": ["read"],
                "revision": membership.revision,
            }
            for membership in await self.memberships.for_user(owner_id)
            if membership.active
            and membership.project_id.hex in row.project_ids
            and "read" in ROLE_PERMISSIONS.get(membership.role, frozenset())
        ]
        if not memberships:
            raise AccessDenied("This personal credential has no current project access")
        principal = Principal(
            user.id,
            user.issuer,
            user.subject,
            user.display_name,
            user.email,
            authentication_method="personal_token",
            kind="service",
            personal_token_id=row.id,
        )
        return PersonalTokenGrant(principal, memberships)

    async def grant(self, token_id: UUID | None, owner_id: UUID) -> PersonalTokenGrant:
        if token_id is None:
            raise AccessDenied("A personal credential identity is required")
        async with self.store._unit():
            return await self._grant(token_id, owner_id)

    async def project_memberships(self, user: Principal) -> list[dict[str, Any]]:
        grant = await self.grant(user.personal_token_id, user.id)
        return grant.memberships

    async def resolve(self, raw: str) -> PersonalTokenGrant:
        if not PERSONAL_TOKEN_PATTERN.fullmatch(raw):
            raise AccessDenied("This personal credential is unavailable")
        token_id = UUID(hex=raw[len(PERSONAL_TOKEN_PREFIX) :].split(".", 1)[0])
        async with self.store._unit():
            candidate = await self.records.find_by_id(token_id)
            if candidate is None:
                hmac.compare_digest(hashlib.sha256(raw.encode()).hexdigest(), "0" * 64)
                raise AccessDenied("This personal credential is unavailable")
            return await self._grant(token_id, candidate.owner_id, hashlib.sha256(raw.encode()).hexdigest())


class PersonalTokenAuthenticationProvider:
    def __init__(self, tokens: PersonalTokens) -> None:
        self.tokens = tokens

    def supports(self, authentication: Authentication) -> bool:
        return authentication.details.get("credential_kind") == "personal_token"

    async def authenticate(self, authentication: Authentication) -> Authentication:
        raw, authentication.credentials = authentication.credentials or "", None
        try:
            grant = await self.tokens.resolve(raw)
        except (ValueError, AccessDenied, KeyError):
            raise BadCredentialsException() from None
        user = grant.principal
        return Authentication(
            principal=user.id.hex,
            authenticated=True,
            credentials=None,
            permissions=["read"],
            details={
                "issuer": user.issuer,
                "subject": user.subject,
                "display_name": user.display_name,
                "email": user.email,
                "personal_token_id": str(user.personal_token_id),
            },
        )
