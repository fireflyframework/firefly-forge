# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Review and save one already-consumed final outcome; the worker owns acceptance."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping

from firefly_forge.final_outcome_models import FinalOutcomeContinuationInputV1
from firefly_forge.invalid_read_continuities import InvalidReadContinuities


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/final-outcome-continuations/requests/{request_id}")
class FinalOutcomeContinuationController:
    def __init__(self, continuities: InvalidReadContinuities) -> None:
        self.continuities = continuities

    async def _action(self, operation: Any, *arguments: Any) -> dict[str, Any]:
        try:
            return dict(await operation(*arguments))
        except KeyError as error:
            raise KeyError("Final outcome continuation not found") from error
        except ValueError as error:
            raise ValueError(
                "Final outcome continuation is unavailable; inspect its retained evidence and current authority"
            ) from error

    @get_mapping("/candidate")
    async def candidate(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self._action(self.continuities.final_outcomes.candidate, run_id, request_id)

    @post_mapping("/actions/{action_id}", status_code=201)
    async def request(
        self,
        run_id: PathVar[str],
        request_id: PathVar[str],
        action_id: PathVar[str],
        body: Body[FinalOutcomeContinuationInputV1],
    ) -> dict[str, Any]:
        return await self._action(self.continuities.final_outcomes.request, run_id, request_id, action_id, body)

    @get_mapping("/status")
    async def status(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self._action(self.continuities.final_outcomes.status, run_id, request_id)
