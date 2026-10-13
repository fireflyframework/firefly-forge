# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Operator-authenticated role edits and explicit durable worker responses."""

from __future__ import annotations

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, put_mapping, request_mapping

from firefly_forge.agent_roles import RoleDocument, RoleRegistry, WorkerResponse
from firefly_forge.community_skills import community_catalog
from firefly_forge.journal import Journal


@rest_controller
@request_mapping("/api/v1")
class AgentRoleController:
    def __init__(self, roles: RoleRegistry, journal: Journal) -> None:
        self.roles = roles
        self.journal = journal

    @get_mapping("/agent-roles")
    def catalog(self) -> dict[str, Any]:
        return {**self.roles.current(), "community_skills": community_catalog(), "tools": self.roles.tools()}

    @put_mapping("/agent-roles")
    def save(self, body: Body[RoleDocument]) -> dict[str, Any]:
        return self.roles.save(body)

    @get_mapping("/runs/{id}/worker-messages")
    async def messages(self, id: PathVar[str]) -> list[dict[str, Any]]:
        return await self.journal.worker_messages(id)

    @post_mapping("/runs/{id}/worker-requests/{request_id}/respond")
    async def respond(self, id: PathVar[str], request_id: PathVar[str], body: Body[WorkerResponse]) -> dict[str, Any]:
        return await self.journal.worker_response(id, request_id, body)
