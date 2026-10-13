# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Factory-administrator review of external connections and scoped project grants."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, put_mapping, request_mapping

from firefly_forge.mcp_registry import ExternalMCPRegistry, MCPConnectionInput, MCPGrantInput, MCPReviewInput
from firefly_forge.projects import Projects
from firefly_forge.store import Store


@rest_controller
@request_mapping("/api/v1/tools/mcp")
class ExternalMCPController:
    def __init__(self, registry: ExternalMCPRegistry, store: Store) -> None:
        self.registry, self.store = registry, store

    @get_mapping("")
    def catalog(self) -> list[dict[str, Any]]:
        return self.registry.catalog()

    @put_mapping("/{connector}")
    def configure(self, connector: PathVar[str], body: Body[MCPConnectionInput]) -> dict[str, Any]:
        return self.registry.configure(connector, body)

    @post_mapping("/{connector}/discover")
    async def discover(self, connector: PathVar[str]) -> dict[str, Any]:
        return await self.registry.discover(connector)

    @post_mapping("/{connector}/review")
    def review(self, connector: PathVar[str], body: Body[MCPReviewInput]) -> dict[str, Any]:
        return self.registry.review(connector, body)

    @get_mapping("/projects/{project}/grants")
    async def grants(self, project: PathVar[str]) -> list[dict[str, Any]]:
        await Projects(self.store).project(project)
        return self.registry.grants(project)

    @post_mapping("/projects/{project}/connectors/{connector}/grants")
    async def grant(self, project: PathVar[str], connector: PathVar[str], body: Body[MCPGrantInput]) -> dict[str, Any]:
        product = await Projects(self.store).project(project)
        if body.repository_id not in {item["repository_id"] for item in product["definition"]["components"]}:
            raise ValueError("The MCP repository must belong to this product project")
        repository = await self.store.repository(body.repository_id)
        return self.registry.grant(project, connector, body, repository=repository)

    @post_mapping("/projects/{project}/grants/{grant}/revoke")
    async def revoke(self, project: PathVar[str], grant: PathVar[str]) -> dict[str, Any]:
        await Projects(self.store).project(project)
        return self.registry.revoke(project, grant)
