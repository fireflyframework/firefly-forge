# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticated product topology configuration and complete component visibility."""

from __future__ import annotations

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, put_mapping, request_mapping

from firefly_forge.project_access import ProjectAccess
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects


@rest_controller
@request_mapping("/api/v1/projects")
class ProjectController:
    def __init__(self, projects: Projects, access: ProjectAccess) -> None:
        self.manager, self.access = projects, access

    @get_mapping("")
    async def projects(self) -> list[dict[str, Any]]:
        return await self.access.filter_projects(await self.manager.projects())

    @post_mapping("", status_code=201)
    async def create(self, body: Body[ProjectDefinition]) -> dict[str, Any]:
        return await self.manager.configure(body)

    @get_mapping("/{id}")
    async def project(self, id: PathVar[str]) -> dict[str, Any]:
        return await self.manager.overview(id)

    @put_mapping("/{id}")
    async def configure(self, id: PathVar[str], body: Body[ProjectDefinition]) -> dict[str, Any]:
        return await self.manager.configure(body, id)

    @get_mapping("/{id}/revisions/{revision}")
    async def revision(self, id: PathVar[str], revision: PathVar[int]) -> dict[str, Any]:
        return await self.manager.revision(id, revision)
