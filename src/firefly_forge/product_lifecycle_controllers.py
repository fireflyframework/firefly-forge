# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Project-authorized product-change previews and immutable definition receipts."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, post_mapping, request_mapping

from firefly_forge.product_lifecycle import FeatureChangeInput, ProductLifecycle
from firefly_forge.project_access import CURRENT_PRINCIPAL


@rest_controller
@request_mapping("/api/v1/repositories/{id}/product/changes")
class ProductLifecycleController:
    def __init__(self, lifecycle: ProductLifecycle) -> None:
        self.lifecycle = lifecycle

    @post_mapping("/preview")
    async def preview(self, id: PathVar[str], body: Body[FeatureChangeInput]) -> dict[str, Any]:
        return await self.lifecycle.preview(id, body)

    @post_mapping("", status_code=201)
    async def apply(self, id: PathVar[str], body: Body[FeatureChangeInput]) -> dict[str, Any]:
        principal = CURRENT_PRINCIPAL.get()
        if principal is None:
            raise PermissionError("Authenticated product change requires a verified identity")
        return await self.lifecycle.apply(id, body, actor_id=principal.id)
