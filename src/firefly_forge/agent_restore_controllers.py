# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticated explicit checkpoint admission and exact ticket readback."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping

from firefly_forge.agent_checkpoint_models import RestoreInput
from firefly_forge.agent_restores import AgentRestores


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/agent-checkpoints/{completion_id}/restores")
class AgentRestoreController:
    def __init__(self, restores: AgentRestores) -> None:
        self.restores = restores

    @post_mapping("")
    async def request(
        self, run_id: PathVar[str], completion_id: PathVar[str], body: Body[RestoreInput]
    ) -> dict[str, Any]:
        return await self.restores.request(run_id, completion_id, body)

    @get_mapping("/{request_id}")
    async def get(self, run_id: PathVar[str], completion_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.restores.get(run_id, completion_id, request_id)


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/agent-checkpoints")
class AgentCheckpointCandidateController:
    def __init__(self, restores: AgentRestores) -> None:
        self.restores = restores

    @get_mapping("/candidates")
    async def candidates(self, run_id: PathVar[str]) -> dict[str, Any]:
        return await self.restores.candidates(run_id)
