# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit legacy adoption and exact original operation readback, never inline execution."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, QueryParam, get_mapping, post_mapping, request_mapping

from firefly_forge.verification_handoffs import VerificationHandoffs
from firefly_forge.verification_models import VerificationAdoption


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/verification-handoffs")
class VerificationHandoffController:
    def __init__(self, handoffs: VerificationHandoffs) -> None:
        self.handoffs = handoffs

    @get_mapping("/candidates")
    async def candidates(self, run_id: PathVar[str], agent: QueryParam[str]) -> dict[str, Any]:
        return await self.handoffs.candidates(run_id, agent)

    @post_mapping("/adoptions", status_code=201)
    async def adopt(self, run_id: PathVar[str], body: Body[VerificationAdoption]) -> dict[str, Any]:
        return await self.handoffs.adopt(run_id, body)

    @get_mapping("/adoptions/{request_id}")
    async def adoption(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.handoffs.adoption(run_id, request_id)
