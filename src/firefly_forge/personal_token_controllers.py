# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owner-session lifecycle endpoints; disclosure is one-time and never cacheable."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, QueryParam, get_mapping, post_mapping, request_mapping
from starlette.responses import JSONResponse

from firefly_forge.identity import Principal
from firefly_forge.personal_token_models import PersonalTokenInput, PersonalTokenRotationInput
from firefly_forge.personal_tokens import PersonalTokens
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied


@rest_controller
@request_mapping("/api/v1/identity/tokens")
class PersonalTokenController:
    def __init__(self, tokens: PersonalTokens) -> None:
        self.tokens = tokens

    @staticmethod
    def _actor() -> Principal:
        actor = CURRENT_PRINCIPAL.get()
        if actor is None:
            raise AccessDenied("Sign in to manage personal credentials")
        return actor

    @get_mapping("")
    async def list(self, after: QueryParam[str | None] = None, limit: QueryParam[int] = 25) -> dict[str, Any]:
        return await self.tokens.page(self._actor(), after=after, limit=limit)

    @post_mapping("", status_code=201)
    async def create(self, body: Body[PersonalTokenInput]) -> JSONResponse:
        result = await self.tokens.create(self._actor(), body)
        return JSONResponse(result.disclose(), status_code=201, headers={"Cache-Control": "private, no-store"})

    @post_mapping("/{token_id}/revoke")
    async def revoke(self, token_id: PathVar[str]) -> dict[str, Any]:
        return await self.tokens.revoke(self._actor(), token_id)

    @post_mapping("/{token_id}/rotate", status_code=201)
    async def rotate(self, token_id: PathVar[str], body: Body[PersonalTokenRotationInput]) -> JSONResponse:
        result = await self.tokens.rotate(self._actor(), token_id, body)
        return JSONResponse(result.disclose(), status_code=201, headers={"Cache-Control": "private, no-store"})
