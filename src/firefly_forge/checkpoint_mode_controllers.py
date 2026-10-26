# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Review and explicitly admit one retained implementation's stronger recovery modes."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping

from firefly_forge.checkpoint_mode_adoptions import CheckpointModeAdoptions
from firefly_forge.checkpoint_mode_models import CheckpointModeAdoptionInput


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/checkpoint-mode-adoptions")
class CheckpointModeAdoptionController:
    def __init__(self, adoptions: CheckpointModeAdoptions) -> None:
        self.adoptions = adoptions

    @get_mapping("/candidate")
    async def candidate(self, run_id: PathVar[str]) -> dict[str, Any]:
        return await self.adoptions.candidate(run_id)

    @post_mapping("/{request_id}", status_code=201)
    async def request(
        self, run_id: PathVar[str], request_id: PathVar[str], body: Body[CheckpointModeAdoptionInput]
    ) -> dict[str, Any]:
        return await self.adoptions.request(run_id, request_id, body)

    @get_mapping("/{request_id}")
    async def get(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.adoptions.get(run_id, request_id)
