# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Project brand kits and executable DevSecOps template previews."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, put_mapping, request_mapping

from firefly_forge.devsecops import ci_catalog, render_ci
from firefly_forge.models import Frontend
from firefly_forge.product_branding import BrandInput, ProductBranding


class CITemplateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["github", "gitlab", "bitbucket"]
    backend: Literal["pyfly", "larafly"] = "pyfly"
    frontend: Frontend = "react"
    image_registry: str = Field(min_length=1, max_length=300)
    signing: bool = False


@rest_controller
@request_mapping("/api/v1")
class ProductController:
    def __init__(self, branding: ProductBranding) -> None:
        self.branding = branding

    @get_mapping("/repositories/{id}/brand")
    async def brand(self, id: PathVar[str]) -> dict[str, Any] | None:
        return await self.branding.current(id)

    @put_mapping("/repositories/{id}/brand")
    async def configure_brand(self, id: PathVar[str], body: Body[BrandInput]) -> dict[str, Any]:
        return await self.branding.configure(id, body)

    @get_mapping("/devsecops/providers")
    def ci_providers(self) -> list[dict[str, Any]]:
        return ci_catalog()

    @post_mapping("/devsecops/templates")
    def ci_templates(self, body: Body[CITemplateInput]) -> dict[str, Any]:
        return {"files": render_ci(**body.model_dump()), "remote_pipeline_verified": False}
