# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Persistent repository role defaults without changing immutable queued executions."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, put_mapping, request_mapping

from firefly_forge.agent_roles import ProjectRoles
from firefly_forge.store import Store


@rest_controller
@request_mapping("/api/v1/repositories/{id}/agent-roles")
class RepositoryRoleController:
    def __init__(self, store: Store) -> None:
        self.store = store

    @get_mapping("")
    async def selection(self, id: PathVar[str]) -> dict[str, Any]:
        return await self.store.repository_roles(id)

    @put_mapping("")
    async def configure(self, id: PathVar[str], body: Body[ProjectRoles]) -> dict[str, Any]:
        return await self.store.set_repository_roles(id, body)
