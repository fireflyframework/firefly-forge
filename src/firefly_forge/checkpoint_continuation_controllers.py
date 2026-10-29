# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Same-ticket checkpoint admission and readback; execution belongs to the worker."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping

from firefly_forge.checkpoint_continuation_models import CheckpointContinuationInputV1
from firefly_forge.invalid_read_continuities import InvalidReadContinuities


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/checkpoint-continuations/requests/{request_id}")
class CheckpointContinuationController:
    def __init__(self, continuities: InvalidReadContinuities) -> None:
        self.continuities = continuities

    async def _action(self, action: Any, *arguments: Any) -> dict[str, Any]:
        try:
            return dict(await action(*arguments))
        except KeyError as exc:
            raise KeyError("Checkpoint continuation not found") from exc
        except ValueError as exc:
            raise ValueError(
                "Checkpoint continuation is unavailable; inspect the retained request and current authority"
            ) from exc

    @get_mapping("/candidate")
    async def candidate(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self._action(self.continuities.checkpoints.candidate, run_id, request_id)

    @post_mapping("/actions/{action_id}", status_code=201)
    async def request(
        self,
        run_id: PathVar[str],
        request_id: PathVar[str],
        action_id: PathVar[str],
        body: Body[CheckpointContinuationInputV1],
    ) -> dict[str, Any]:
        return await self._action(self.continuities.checkpoints.request, run_id, request_id, action_id, body)

    @get_mapping("/status")
    async def status(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self._action(self.continuities.checkpoints.status, run_id, request_id)
