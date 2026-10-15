# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Current-authorized Factory admission and exact readback; controllers never execute work."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping

from firefly_forge.runner_continuations import RunnerContinuationInput, RunnerContinuations


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/runner-continuations")
class RunnerContinuationController:
    def __init__(self, continuations: RunnerContinuations) -> None:
        self.continuations = continuations

    @post_mapping("")
    async def request(self, run_id: PathVar[str], body: Body[RunnerContinuationInput]) -> dict[str, Any]:
        return await self.continuations.request(run_id, body)

    @get_mapping("/{request_id}")
    async def get(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.continuations.get(run_id, request_id)
