# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Review and export infrastructure recipes before explicit deployment."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping
from starlette.responses import FileResponse

from firefly_forge.cloud import CloudRecipeInput, CloudRecipes


class CloudTargetSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")


@rest_controller
@request_mapping("/api/v1/cloud")
class CloudController:
    def __init__(self, recipes: CloudRecipes) -> None:
        self.recipes = recipes

    @get_mapping("/providers")
    def catalog(self) -> list[dict[str, Any]]:
        return self.recipes.catalog()

    @post_mapping("/recipes/render")
    async def render(self, body: Body[CloudRecipeInput]) -> dict[str, Any]:
        return await self.recipes.render(body)

    @post_mapping("/recipes", status_code=201)
    async def save(self, body: Body[CloudRecipeInput]) -> dict[str, Any]:
        return await self.recipes.save(body)

    @get_mapping("/recipes/{id}/archive.zip")
    async def export(self, id: PathVar[str]) -> FileResponse:
        return FileResponse(
            await self.recipes.export(id), filename="deployment-recipe.zip", media_type="application/zip"
        )

    @post_mapping("/recipes/{id}/configure-target")
    async def configure(self, id: PathVar[str], body: Body[CloudTargetSelection]) -> dict[str, Any]:
        return await self.recipes.configure_target(id, body.target_id)
