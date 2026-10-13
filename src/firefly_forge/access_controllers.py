# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticated identity display and project-member administration."""

from typing import Any

from pydantic import BaseModel, ConfigDict
from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, QueryParam, get_mapping, put_mapping, request_mapping
from sqlalchemy import or_, select

from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserEntity, ForgeIdentityUserRepository
from firefly_forge.project_access import CURRENT_PRINCIPAL, ROLE_PERMISSIONS, ProjectAccess, Role
from firefly_forge.store import Store, identifier


class MembershipInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role | None


@rest_controller
@request_mapping("/api/v1")
class AccessController:
    def __init__(self, access: ProjectAccess, store: Store) -> None:
        self.access, self.store = access, store

    @staticmethod
    def _contact(user: ForgeIdentityUserEntity | None) -> dict[str, Any]:
        return {
            "email": (user.email or user.contact_email) if user else None,
            "contact_email": user.contact_email if user else None,
            "email_verified": bool(user and user.email is not None),
        }

    @get_mapping("/auth/session")
    async def session(self) -> dict[str, Any]:
        user = CURRENT_PRINCIPAL.get()
        assert user is not None
        return {
            "authenticated": True,
            "user": {
                "id": user.id.hex,
                "display_name": user.display_name,
                "email": user.email,
                "authentication_method": user.authentication_method,
            },
            "global_admin": await self.access.current_administrator(user),
            "memberships": await self.access.memberships(user),
            "roles": {role: sorted(permissions) for role, permissions in ROLE_PERMISSIONS.items()},
        }

    @get_mapping("/identity/users")
    async def users(self) -> list[dict[str, Any]]:
        async with self.store._unit() as session:
            rows = await session.scalars(
                select(ForgeIdentityUserEntity).order_by(ForgeIdentityUserEntity.id).limit(200)
            )
            return [
                dict(id=row.id.hex, display_name=row.display_name, enabled=row.enabled, **self._contact(row))
                for row in rows
            ]

    @get_mapping("/projects/{id}/members")
    async def members(self, id: PathVar[str]) -> list[dict[str, Any]]:
        async with self.store._unit():
            rows = await self.access.records.for_project(identifier(id))
            result = []
            for row in rows:
                user = await ForgeIdentityUserRepository().find_by_id(row.user_id)
                result.append(
                    dict(
                        user_id=row.user_id.hex,
                        display_name=user.display_name if user else None,
                        **self._contact(user),
                        role=row.role,
                        active=row.active,
                        revision=row.revision,
                    )
                )
            return result

    @put_mapping("/projects/{id}/members/{user_id}")
    async def configure(self, id: PathVar[str], user_id: PathVar[str], body: Body[MembershipInput]) -> dict[str, Any]:
        async with self.store._unit():
            user = await ForgeIdentityUserRepository().find_by_id(identifier(user_id))
            if user is None:
                raise KeyError("Registered user not found; ask them to sign in first")
            principal = Principal(user.id, user.issuer, user.subject, user.display_name, user.email)
        await self.access.set_membership(id, principal, body.role)
        return {"project_id": id, "user_id": user_id, "role": body.role}

    @get_mapping("/projects/{id}/eligible-users")
    async def eligible(self, id: PathVar[str], query: QueryParam[str] = "") -> list[dict[str, Any]]:
        query = query.strip()
        if not 3 <= len(query) <= 120:
            raise ValueError("Enter between 3 and 120 characters to find a registered user")
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with self.store._unit() as session:
            rows = await session.scalars(
                select(ForgeIdentityUserEntity)
                .where(
                    ForgeIdentityUserEntity.enabled.is_(True),
                    or_(
                        ForgeIdentityUserEntity.email.ilike(escaped, escape="\\"),
                        ForgeIdentityUserEntity.contact_email.ilike(escaped, escape="\\"),
                        ForgeIdentityUserEntity.display_name.ilike("%" + escaped + "%", escape="\\"),
                    ),
                )
                .order_by(ForgeIdentityUserEntity.display_name, ForgeIdentityUserEntity.id)
                .limit(20)
            )
            return [
                dict(id=row.id.hex, display_name=row.display_name, enabled=row.enabled, **self._contact(row))
                for row in rows
            ]
