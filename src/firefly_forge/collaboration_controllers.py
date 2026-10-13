# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native PyFly operator and signed-webhook collaboration endpoints."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, QueryParam, get_mapping, post_mapping, put_mapping, request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from firefly_forge.collaboration import Collaboration, CollaborationSettings, ProductDefinition
from firefly_forge.models import Backend, Frontend
from firefly_forge.product_architecture import ProductArchitectureService


class IssueDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=250)
    body: str = Field(default="", max_length=50000)
    idempotency_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,100}$")


class FeedbackReconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["posted", "not_posted", "completed", "not_completed"]
    note: str = Field(min_length=1, max_length=2000)


class ArchitectureProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_definition: ProductDefinition
    backend: Backend | None = None
    frontend: Frontend | None = None


@rest_controller
@request_mapping("/api/v1")
class CollaborationController:
    def __init__(self, collaboration: Collaboration, architectures: ProductArchitectureService | None = None) -> None:
        self.collaboration = collaboration
        self.architectures = architectures or ProductArchitectureService()

    @post_mapping("/architecture/propose")
    def propose(self, body: Body[ArchitectureProposal]) -> dict[str, Any]:
        return self.architectures.propose(
            body.product_definition.model_dump(), backend=body.backend, frontend=body.frontend
        )

    @get_mapping("/repositories/{id}/collaboration")
    async def settings(self, id: PathVar[str]) -> dict[str, Any]:
        return await self.collaboration.settings(id)

    @put_mapping("/repositories/{id}/collaboration")
    async def configure(self, id: PathVar[str], body: Body[CollaborationSettings]) -> dict[str, Any]:
        return await self.collaboration.configure(id, body)

    @get_mapping("/repositories/{id}/product")
    async def product(self, id: PathVar[str]) -> dict[str, Any] | None:
        return await self.collaboration.product(id)

    @put_mapping("/repositories/{id}/product")
    async def save_product(self, id: PathVar[str], body: Body[ProductDefinition]) -> dict[str, Any]:
        return await self.collaboration.save_product(id, body)

    @post_mapping("/repositories/{id}/issues", status_code=201)
    async def create_issue(self, id: PathVar[str], body: Body[IssueDraft]) -> dict[str, Any]:
        return await self.collaboration.create_issue(id, body.title, body.body, idempotency_key=body.idempotency_key)

    @get_mapping("/collaboration/receipts")
    async def receipts(self, repository_id: QueryParam[str | None] = None) -> list[dict[str, Any]]:
        return await self.collaboration.receipts(repository_id)

    @post_mapping("/collaboration/receipts/{id}/process")
    async def process(self, id: PathVar[str]) -> dict[str, Any]:
        return await self.collaboration.process(id)

    @post_mapping("/collaboration/receipts/{id}/reconcile")
    async def reconcile(self, id: PathVar[str], body: Body[FeedbackReconciliation]) -> dict[str, Any]:
        return await self.collaboration.reconcile(id, body.outcome, body.note)

    @post_mapping("/webhooks/{provider}/{repository_id}", status_code=202)
    async def webhook(self, provider: PathVar[str], repository_id: PathVar[str], request: Request) -> Response:
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 100000:
                return JSONResponse({"detail": "Webhook body exceeds 100 KB"}, status_code=413)
            raw.extend(chunk)
        receipt = await self.collaboration.receive(provider, repository_id, bytes(raw), request.headers)
        return JSONResponse(receipt, status_code=202)
