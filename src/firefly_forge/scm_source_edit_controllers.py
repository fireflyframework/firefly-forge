# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Separate native administrator consent for an exact existing source-only task."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping

from firefly_forge.scm_source_edit import SCMSourceEditAuthority
from firefly_forge.scm_source_edit_models import SCMSourceEditGrantInputV1


@rest_controller
@request_mapping("/api/v1/projects/{project_id}/repositories/{repository_id}/scm-polling/source-edit-grants")
class SCMSourceEditController:
    def __init__(self, authority: SCMSourceEditAuthority) -> None:
        self.authority = authority

    @post_mapping("", status_code=201)
    async def configure(
        self, project_id: PathVar[str], repository_id: PathVar[str], body: Body[SCMSourceEditGrantInputV1]
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
