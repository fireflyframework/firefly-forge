# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fail-closed project authorization, separate from agent execution roles."""

from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast

from pyfly.data.relational.sqlalchemy import LockMode
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from firefly_forge.access_persistence import (
    ForgeAccessEventEntity,
    ForgeAccessEventRepository,
    ForgeMembershipEntity,
    ForgeMembershipRepository,
)
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserEntity, ForgeIdentityUserRepository
from firefly_forge.project_persistence import ForgeProjectRepository
from firefly_forge.store import identifier, now

Permission = Literal["read", "edit", "execute", "review", "release", "members"]
Role = Literal["owner", "maintainer", "developer", "reviewer", "viewer"]
CURRENT_PRINCIPAL: ContextVar[Principal | None] = ContextVar("forge_principal", default=None)
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset({"read", "edit", "execute", "review", "release", "members"}),
    "maintainer": frozenset({"read", "edit", "execute", "review", "release"}),
    "developer": frozenset({"read", "edit", "execute"}),
    "reviewer": frozenset({"read", "review"}),
    "viewer": frozenset({"read"}),
}


class AccessDenied(Exception):
    pass


class AccessTransactions(Protocol):
    schema: str

    def _unit(self) -> AbstractAsyncContextManager[AsyncSession]: ...


class ProjectAccess:
    def __init__(self, store: AccessTransactions, records: ForgeMembershipRepository | None = None) -> None:
        self.store, self.records = store, records or ForgeMembershipRepository()

    @staticmethod
    def permits(role: str, permission: str) -> bool:
        return permission in ROLE_PERMISSIONS.get(role, frozenset())

    @staticmethod
    def is_admin(user: Principal) -> bool:
        return user.authentication_method != "scm_mapping" and (
            user.authentication_method == "local_bootstrap" or "forge-admin" in user.api_roles
        )

    async def current_administrator(self, user: Principal) -> bool:
        if not self.is_admin(user):
            return False
        try:
            return await self.authorize_actor(self.actor_snapshot(user), None, "members")
        except AccessDenied:
            return False

    async def memberships(self, user: Principal) -> list[dict[str, Any]]:
        async with self.store._unit():
            identity = await ForgeIdentityUserRepository().find_by_id(user.id)
            if identity is not None and not identity.enabled:
                return []
            return [
                dict(
                    project_id=row.project_id.hex,
                    role=row.role,
                    revision=row.revision,
                    permissions=sorted(ROLE_PERMISSIONS[row.role]),
                )
                for row in await self.records.for_user(user.id)
            ]

    async def require_project(self, user: Principal, project_id: str, permission: Permission) -> None:
        if user.authentication_method == "scm_mapping":
            await self.authorize_actor(self.actor_snapshot(user), project_id, permission)
            return
        if await self.current_administrator(user):
            return
        memberships = await self.memberships(user)
        if not any(row["project_id"] == project_id and self.permits(row["role"], permission) for row in memberships):
            raise AccessDenied("Your project role does not allow this action")

    async def require_repository(self, user: Principal, repository_id: str, permission: Permission) -> None:
        if user.authentication_method == "scm_mapping":
            delegation = user.scm_delegation or {}
            if delegation.get("repository_id") != repository_id:
                raise AccessDenied("SCM delegation does not authorize this repository")
            await self.authorize_actor(self.actor_snapshot(user), delegation.get("project_id"), permission)
            return
        if await self.current_administrator(user):
            return
        from firefly_forge.project_persistence import ForgeProjectEntity

        async with self.store._unit() as session:
            projects = [
                value.hex
                for value in await session.scalars(
                    select(ForgeProjectEntity.id).where(
                        ForgeProjectEntity.data["definition"]["components"].contains([{"repository_id": repository_id}])
                    )
                )
            ]
        allowed = {row["project_id"] for row in await self.memberships(user) if self.permits(row["role"], permission)}
        # Repository-level definitions/configuration affect every project sharing that repository.
        if not projects or (
            not bool(allowed.intersection(projects)) if permission == "read" else not set(projects) <= allowed
        ):
            raise AccessDenied("Your project role does not allow access to this repository")

    async def set_membership(self, project_id: str, user: Principal, role: Role | None) -> None:
        if role is not None and role not in ROLE_PERMISSIONS:
            raise ValueError("Choose a supported project role")
        async with self.store._unit() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": self.store.schema + ":members:" + project_id},
            )
            project = await ForgeProjectRepository().find_by_id(identifier(project_id))
            if project is None:
                raise KeyError("Project not found")
            identities = ForgeIdentityUserRepository()
            identity = await identities.find_by_id(user.id, lock=LockMode.PESSIMISTIC_WRITE)
            if identity is None:
                await identities.save(
                    ForgeIdentityUserEntity(
                        id=user.id,
                        issuer=user.issuer,
                        subject=user.subject,
                        display_name=user.display_name,
                        email=user.email,
                    )
                )
            rows = await self.records.for_project(project.id)
            row = next((row for row in rows if row.user_id == user.id), None)
            if (
                row
                and row.active
                and row.role == "owner"
                and role != "owner"
                and not any(other.user_id != user.id and other.active and other.role == "owner" for other in rows)
            ):
                raise ValueError("Assign another project owner before removing the last owner")
            previous = row.role if row and row.active else None
            if row:
                row.active, row.role, row.revision = role is not None, role or row.role, row.revision + 1
            elif role is not None:
                row = ForgeMembershipEntity(project_id=project.id, user_id=user.id, role=role)
            if row is not None:
                await self.records.save(row)
                actor = CURRENT_PRINCIPAL.get()
                await ForgeAccessEventRepository().save(
                    ForgeAccessEventEntity(
                        project_id=project.id,
                        data={
                            "kind": "membership_changed",
                            "user_id": user.id.hex,
                            "previous_role": previous,
                            "role": role,
                            "revision": row.revision,
                            "actor_id": actor.id.hex if actor else None,
                            "at": now(),
                        },
                    )
                )

    async def filter_projects(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        user = CURRENT_PRINCIPAL.get()
        if user is None or await self.current_administrator(user):
            return rows
        allowed = {row["project_id"] for row in await self.memberships(user)}
        return [row for row in rows if row["id"] in allowed]

    async def filter_runs(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        user = CURRENT_PRINCIPAL.get()
        if user is None or await self.current_administrator(user):
            return rows
        allowed = {row["project_id"] for row in await self.memberships(user)}
        return [row for row in rows if row.get("project_id") in allowed]

    async def filter_repositories(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        user = CURRENT_PRINCIPAL.get()
        if user is None or await self.current_administrator(user):
            return rows
        result = []
        for row in rows:
            try:
                await self.require_repository(user, row["id"], "read")
            except AccessDenied:
                continue
            result.append(row)
        return result

    async def authorize_execution(self, run: dict[str, Any], permission: Permission = "execute") -> None:
        actor = run.get("execution_actor")
        if not actor:
            return  # Pre-identity and explicitly internal executions keep their recorded admission contract.
        self.require_provider_isolation(run, run.get("agent", "agentic"))
        self.require_provider_isolation(run, run.get("assurance", {}).get("review_agent"))
        administrator = await self.authorize_actor(actor, run.get("project_id"), permission)
        if not administrator and (
            run.get("agent") in {"codex", "claude"}
            or run.get("assurance", {}).get("review_agent") in {"codex", "claude"}
            or (run.get("sandbox_snapshot") or {}).get("mode") == "host"
        ):
            raise AccessDenied("Host coding providers and runners require current factory administrator authority")

    @staticmethod
    def actor_snapshot(user: Principal) -> dict[str, Any]:
        if user.authentication_method == "scm_mapping":
            import copy

            return {
                "user_id": user.id.hex,
                "authentication_method": "scm_mapping",
                "global_admin": False,
                "api_audience": None,
                "scm_delegation": copy.deepcopy(user.scm_delegation),
            }
        return {
            "user_id": user.id.hex,
            "authentication_method": user.authentication_method,
            "global_admin": ProjectAccess.is_admin(user),
            "api_audience": user.api_audience,
        }

    async def authorize_actor(
        self, actor: dict[str, Any] | None, project_id: str | None, permission: Permission = "release"
    ) -> bool:
        if not actor:
            return True
        if actor.get("authentication_method") == "scm_mapping":
            from firefly_forge.scm_actor_authority import SCMActorAuthority
            from firefly_forge.store import Store

            await SCMActorAuthority(cast(Store, self.store)).authorize_snapshot(actor, project_id, permission)
            return False
        async with self.store._unit():
            user = await ForgeIdentityUserRepository().find_by_id(identifier(actor["user_id"]))
            if actor["authentication_method"] == "local_bootstrap":
                if user is not None and not user.enabled:
                    raise AccessDenied("The execution identity is suspended")
                return True
            if user is None or not user.enabled:
                raise AccessDenied("The execution identity is unavailable or suspended")
            principal = Principal(user.id, user.issuer, user.subject)
            administrator = (
                bool(actor.get("global_admin"))
                and bool(actor.get("api_audience"))
                and actor["api_audience"] == user.api_roles_audience
                and user.api_roles_issued_at is not None
                and user.api_roles_expires_at is not None
                and user.api_roles_expires_at > datetime.now(UTC)
                and "forge-admin" in (user.api_roles or ())
            )
        if administrator:
            return True
        if not project_id:
            raise AccessDenied("An authorized execution requires a project")
        await self.require_project(principal, project_id, permission)
        return False

    @staticmethod
    def require_provider_isolation(run: dict[str, Any], provider: str | None) -> None:
        actor = run.get("execution_actor")
        if (
            actor
            and not actor.get("global_admin")
            and actor.get("authentication_method") != "local_bootstrap"
            and provider in {"codex", "claude"}
        ):
            raise ValueError("Direct host coding CLIs require a factory administrator; choose an isolated API agent")
