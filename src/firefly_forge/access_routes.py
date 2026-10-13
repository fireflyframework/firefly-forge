# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Deny unclassified routes and authorize resource identities before controller dispatch."""

from typing import Any

from starlette.requests import Request

from firefly_forge.identity import Principal
from firefly_forge.project_access import AccessDenied, Permission, ProjectAccess
from firefly_forge.store import Store

SAFE = {"GET", "HEAD", "OPTIONS"}
PUBLIC_CATALOGS = {
    "health",
    "openapi.json",
    "profiles",
    "workflow",
    "skills",
    "policies",
    "agent-roles",
    "devsecops/providers",
}


class AccessRoutes:
    def __init__(self, access: ProjectAccess, store: Store) -> None:
        self.access, self.store = access, store

    async def authorize(self, user: Principal, request: Request) -> None:
        if await self.access.current_administrator(user):
            return
        path = request.url.path.removeprefix("/api/v1/").strip("/")
        parts = path.split("/")
        safe = request.method in SAFE
        if path == "auth/session" or safe and path in PUBLIC_CATALOGS:
            return
        if safe and path in {"projects", "repositories", "runs"}:
            return  # Their controllers filter list results using the same authenticated principal.
        body: dict[str, Any] = {}
        if not safe:
            try:
                # PyFly replays a filter-consumed body to the native controller from this scope key.
                request.scope["pyfly.request_body"] = await request.body()
                value = await request.json()
            except ValueError:
                value = {}
            if isinstance(value, dict):
                body = value
        if parts[0] == "projects" and len(parts) >= 2:
            permission: Permission = "read" if safe else "edit"
            if len(parts) > 2 and parts[2] in {"members", "eligible-users"}:
                permission = "members"
            elif len(parts) > 2 and parts[2] == "campaigns" and not safe:
                permission = "release" if body.get("publish") else "execute"
                if len(parts) >= 5 and parts[4] in {"resume", "retry"}:
                    from firefly_forge.project_campaign_persistence import ForgeCampaignRepository
                    from firefly_forge.store import identifier

                    async with self.store._unit():
                        campaign = await ForgeCampaignRepository().find_by_id(identifier(parts[3]))
                        if campaign is None or campaign.project_id.hex != parts[1]:
                            raise AccessDenied("Campaign not available in this project")
                        if campaign.data["launch"].get("publish"):
                            permission = "release"
            elif path.endswith("/review") and not safe:
                permission = "review"
            await self.access.require_project(user, parts[1], permission)
            if not safe and "components" in body:
                for component in body["components"]:
                    await self.access.require_repository(user, component["repository_id"], "edit")
            return
        if parts[0] == "repositories" and len(parts) >= 2:
            permission = "read" if safe else "edit"
            # Execution policies/channel trusted identities can grant server-side authority.
            if len(parts) > 2 and parts[2] in {"policies", "collaboration", "agent-roles"} and not safe:
                permission = "members"
            await self.access.require_repository(user, parts[1], permission)
            if not safe and (
                body.get("functional_reviewed")
                or (body.get("design_review") or {}).get("reviewed")
                or (body.get("architecture") or {}).get("reviewed")
            ):
                await self.access.require_repository(user, parts[1], "review")
            if not safe and body.get("project_id"):
                await self.access.require_project(user, body["project_id"], "edit")
            return
        if parts[0] == "runs":
            if len(parts) == 1 and not safe:
                project_id = body.get("project_id")
                if not project_id:
                    raise AccessDenied("Select an authorized product project before starting work")
                await self.access.require_project(user, project_id, "release" if body.get("publish") else "execute")
                return
            if len(parts) >= 2:
                run = await self.store.get_run(parts[1])
                permission = "read" if safe else "release" if parts[-1] in {"publish", "deploy"} else "execute"
                if not run.get("project_id"):
                    raise AccessDenied("Unassigned executions require a factory administrator")
                await self.access.require_project(user, run["project_id"], permission)
                if not safe and permission == "execute" and run.get("publish"):
                    await self.access.require_project(user, run["project_id"], "release")
                return
        if path == "knowledge/explore" and safe:
            await self.access.require_repository(user, request.query_params.get("repository_id", ""), "read")
            return
        if path == "knowledge/search" and not safe:
            await self.access.require_repository(user, body.get("repository_id", ""), "read")
            return
        if path in {"architecture/propose", "devsecops/templates"} and not safe:
            return  # Pure previews: no stored project/configuration access or execution.
        raise AccessDenied("This action requires a factory administrator")
