# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exact completion evidence routes; these controllers never submit model requests."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, QueryParam, get_mapping, post_mapping, request_mapping

from firefly_forge.bridge_completion_models import ObservationInput
from firefly_forge.bridge_completions import BridgeCompletions


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/bridge-requests")
class BridgeCompletionController:
    def __init__(self, completions: BridgeCompletions) -> None:
        self.completions = completions

    @get_mapping("")
    async def list(self, run_id: PathVar[str], after: QueryParam[int] = 0) -> dict[str, Any]:
        return await self.completions.list(run_id, after)

    @get_mapping("/{request_id}")
    async def exact(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.completions.exact(run_id, request_id)

    @post_mapping("/{request_id}/observations", status_code=201)
    async def observe(
        self, run_id: PathVar[str], request_id: PathVar[str], body: Body[ObservationInput]
    ) -> dict[str, Any]:
        return await self.completions.observe(run_id, request_id, body)

    @get_mapping("/{request_id}/observations/{observation_id}")
    async def observation(
        self, run_id: PathVar[str], request_id: PathVar[str], observation_id: PathVar[str]
    ) -> dict[str, Any]:
        return await self.completions.observation(run_id, request_id, observation_id)
