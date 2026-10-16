# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native administration and exact readback for explicitly consented product edits."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping

from firefly_forge.scm_product_change_models import SCMProductChangeGrantInputV1
from firefly_forge.scm_product_change_service import SCMProductChangeAuthority


@rest_controller
@request_mapping("/api/v1/projects/{project_id}/repositories/{repository_id}")
class SCMProductChangeController:
    def __init__(self, authority: SCMProductChangeAuthority) -> None:
        self.authority = authority

    @post_mapping("/scm-product-change-grants", status_code=201)
    async def configure(
        self, project_id: PathVar[str], repository_id: PathVar[str], body: Body[SCMProductChangeGrantInputV1]
    ) -> dict[str, Any]:
        return await self.authority.configure(project_id, repository_id, body)

    @get_mapping("/scm-product-change-grants/requests/{request_id}")
    async def exact(
        self, project_id: PathVar[str], repository_id: PathVar[str], request_id: PathVar[str]
    ) -> dict[str, Any]:
        return await self.authority.get(project_id, repository_id, request_id)

    @get_mapping("/scm-product-change-grants/current/{binding_request_id}")
    async def current(
        self, project_id: PathVar[str], repository_id: PathVar[str], binding_request_id: PathVar[str]
    ) -> dict[str, Any]:
        return await self.authority.current(project_id, repository_id, binding_request_id)

    @get_mapping("/scm-product-changes/requests/{request_id}")
    async def operation(
        self, project_id: PathVar[str], repository_id: PathVar[str], request_id: PathVar[str]
    ) -> dict[str, Any]:
        return await self.authority.operation(project_id, repository_id, request_id)
