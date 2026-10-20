# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit polling-only continuation consent and original-result reads."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, QueryParam, get_mapping, post_mapping, request_mapping

from firefly_forge.scm_poll_resume import SCMPollResumeAuthority
from firefly_forge.scm_poll_resume_models import SCMPollResumeGrantInputV3


@rest_controller
@request_mapping("/api/v1/projects/{project_id}/repositories/{repository_id}/scm-polling/resume-grants")
class SCMPollResumeController:
    def __init__(self, authority: SCMPollResumeAuthority) -> None:
        self.authority = authority

    @post_mapping("", status_code=201)
    async def configure(
        self, project_id: PathVar[str], repository_id: PathVar[str], body: Body[SCMPollResumeGrantInputV3]
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

    @get_mapping("/review")
    async def review(
        self,
        project_id: PathVar[str],
        repository_id: PathVar[str],
        binding_request_id: QueryParam[str],
        run_id: QueryParam[str],
    ) -> dict[str, Any]:
        return await self.authority.review(project_id, repository_id, binding_request_id, run_id)
