# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Operator-authenticated role edits and explicit durable worker responses."""

from __future__ import annotations

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, put_mapping, request_mapping

from firefly_forge.agent_lifecycle import lifecycle_catalog
from firefly_forge.agent_roles import ProjectRoles, RoleDocument, RoleRegistry, WorkerResponse
from firefly_forge.community_skills import community_catalog
from firefly_forge.diagnostic_recipes import profile_available
from firefly_forge.journal import Journal
from firefly_forge.knowledge import KnowledgeManager
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.runner_configuration import RunnerConfiguration
from firefly_forge.sandbox_models import capture_sandbox
from firefly_forge.store import Store
from firefly_forge.workspace_tools import tool_catalog


@rest_controller
@request_mapping("/api/v1")
class AgentRoleController:
    def __init__(
        self,
        roles: RoleRegistry,
        journal: Journal,
        store: Store,
        knowledge: KnowledgeManager,
        runners: RunnerConfiguration,
    ) -> None:
        self.roles, self.journal, self.store, self.knowledge = roles, journal, store, knowledge
        self.runners = runners

    @get_mapping("/agent-roles")
    async def catalog(self) -> dict[str, Any]:
        result = {**self.roles.current(), "community_skills": community_catalog(), "tools": self.roles.tools()}
        await self.store.admit_credentials(result)
        return result

    async def _lifecycle(self, repository_id: str | None = None) -> dict[str, Any]:
        repo = await self.store.repository(repository_id) if repository_id else None
        router = self.store.task_router
        tools = self.roles.tools()
        if repository_id:
            configuration = (await self.runners.configuration(repository_id))["sandbox"]
            tools = tool_catalog(
                {
                    "execute_command": True,
                    "profile_python": profile_available(capture_sandbox(configuration))
                    if configuration.get("mode") != "pool"
                    else False,
                    "browser_check": bool(configuration.get("browser")),
                    "browser_journey": bool(configuration.get("browser")),
                    "internet_search": bool(
                        configuration.get("internet_search") and self.runners.search.configuration()["configured"]
                    ),
                }
            )
            for tool in tools:
                if tool["requires"] and tool["availability"] == "available":
                    tool.update(
                        availability="worker_unverified",
                        reason="Configured grant; execution worker must validate runtime availability",
                    )
        result = lifecycle_catalog(
            self.roles,
            overrides=ProjectRoles.model_validate(repo.get("roles") or {}) if repo else None,
            policy=self.store.policies.selection(repo.get("skills") if repo else None),
            knowledge=self.knowledge.public(),
            routing=router.current() if router else None,
            profiles=router.profiles.catalog() if router else None,
            repository_id=repository_id,
            assistant_policy=self.store.policies.current(),
            tools=tools,
        )
        await self.store.admit_credentials(result)
        actor = CURRENT_PRINCIPAL.get()
        access = ProjectAccess(self.store)
        admin = await access.current_administrator(actor) if actor else False
        can_edit = admin
        if actor and repository_id and not admin:
            try:
                await access.require_repository(actor, repository_id, "members")
                can_edit = True
            except AccessDenied:
                pass
        result["permissions"] = {"edit_registry": admin, "edit_project": can_edit if repository_id else False}
        return result

    @get_mapping("/agent-roles/lifecycle")
    async def lifecycle(self) -> dict[str, Any]:
        return await self._lifecycle()

    @get_mapping("/repositories/{id}/agent-roles/lifecycle")
    async def project_lifecycle(self, id: PathVar[str]) -> dict[str, Any]:
        return await self._lifecycle(id)

    @put_mapping("/agent-roles")
    async def save(self, body: Body[RoleDocument]) -> dict[str, Any]:
        await self.store.admit_credentials(body.model_dump(mode="json"))
        return self.roles.save(body)

    @get_mapping("/runs/{id}/worker-messages")
    async def messages(self, id: PathVar[str]) -> list[dict[str, Any]]:
        return await self.journal.worker_messages(id)

    @post_mapping("/runs/{id}/worker-requests/{request_id}/respond")
    async def respond(self, id: PathVar[str], request_id: PathVar[str], body: Body[WorkerResponse]) -> dict[str, Any]:
        return await self.journal.worker_response(id, request_id, body)
