# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit current-authority SCM mapping writes and exact read-only recovery."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, QueryParam, get_mapping, post_mapping, request_mapping

from firefly_forge.scm_actor_authority import SCMActorAuthority
from firefly_forge.scm_authority_models import SCMActorBindingInputV1


@rest_controller
@request_mapping("/api/v1/projects/{project_id}/repositories/{repository_id}/scm-actor-bindings")
class SCMActorAuthorityController:
    def __init__(self, authority: SCMActorAuthority) -> None:
        self.authority = authority

    @get_mapping("/candidates")
    async def candidates(self, project_id: PathVar[str], repository_id: PathVar[str]) -> dict[str, Any]:
        return await self.authority.candidates(project_id, repository_id)

    @post_mapping("", status_code=201)
    async def configure(
        self,
        project_id: PathVar[str],
        repository_id: PathVar[str],
        body: Body[SCMActorBindingInputV1],
    ) -> dict[str, Any]:
        return await self.authority.configure(project_id, repository_id, body)

    @get_mapping("")
    async def history(
        self,
        project_id: PathVar[str],
        repository_id: PathVar[str],
        cursor: QueryParam[str | None] = None,
    ) -> dict[str, Any]:
        return await self.authority.list(project_id, repository_id, cursor)

    @get_mapping("/requests/{request_id}")
    async def exact(
        self,
        project_id: PathVar[str],
        repository_id: PathVar[str],
        request_id: PathVar[str],
    ) -> dict[str, Any]:
        return await self.authority.get(project_id, repository_id, request_id)
