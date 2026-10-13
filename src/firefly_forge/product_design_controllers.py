# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticated native PyFly customer wireframe preview and saved bundle lookup."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict
from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping

from firefly_forge.collaboration import ProductDefinition
from firefly_forge.product_design import ProductDesign


class DesignPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_definition: ProductDefinition


@rest_controller
@request_mapping("/api/v1/repositories")
class ProductDesignController:
    def __init__(self, design: ProductDesign) -> None:
        self.design = design

    @post_mapping("/{id}/design/preview")
    async def preview(self, id: PathVar[str], body: Body[DesignPreview]) -> dict[str, Any]:
        return await self.design.preview(id, body.product_definition)

    @get_mapping("/{id}/design/{digest}")
    async def saved(self, id: PathVar[str], digest: PathVar[str]) -> dict[str, Any]:
        return await self.design.load(id, digest)
