# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fresh native administrator consent and exact read-only V2 grant recovery."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping

from firefly_forge.scm_resume_models import SCMResumeGrantInputV2
from firefly_forge.scm_resume_service import SCMResumeAuthority


@rest_controller
@request_mapping("/api/v1/projects/{project_id}/repositories/{repository_id}/scm-resume-grants")
class SCMResumeAuthorityController:
    def __init__(self, authority: SCMResumeAuthority) -> None:
        self.authority = authority

    @post_mapping("", status_code=201)
    async def configure(
        self, project_id: PathVar[str], repository_id: PathVar[str], body: Body[SCMResumeGrantInputV2]
    ) -> dict[str, Any]:
        return await self.authority.configure(project_id, repository_id, body)

    @get_mapping("/requests/{request_id}")
    async def exact(
        self, project_id: PathVar[str], repository_id: PathVar[str], request_id: PathVar[str]
    ) -> dict[str, Any]:
        return await self.authority.get(project_id, repository_id, request_id)

    @get_mapping("/current/{binding_request_id}")
    async def current(
        self, project_id: PathVar[str], repository_id: PathVar[str], binding_request_id: PathVar[str]
    ) -> dict[str, Any]:
        return await self.authority.current(project_id, repository_id, binding_request_id)
