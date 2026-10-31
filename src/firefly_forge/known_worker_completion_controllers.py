# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Hash-only known-worker completion candidates and local settlement receipts."""

from typing import Any

from pydantic import RootModel
from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping

from firefly_forge.known_worker_completion_models import ConsumedTerminalInputV2, WorkerCompletionInputV1
from firefly_forge.known_worker_completions import KnownWorkerCompletions


class WorkerCompletionBody(RootModel[WorkerCompletionInputV1 | ConsumedTerminalInputV2]):
    """Decode the versioned wire body through the native model reader."""


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/worker-completion-settlements")
class KnownWorkerCompletionController:
    def __init__(self, completions: KnownWorkerCompletions) -> None:
        self.completions = completions

    @get_mapping("/candidate")
    async def candidate(self, run_id: PathVar[str]) -> dict[str, Any]:
        return await self.completions.candidate(run_id)

    @post_mapping("/{request_id}", status_code=201)
    async def request(
        self,
        run_id: PathVar[str],
        request_id: PathVar[str],
        body: Body[WorkerCompletionBody],
    ) -> dict[str, Any]:
        return await self.completions.request(run_id, request_id, body.root)

    @get_mapping("/{request_id}")
    async def get(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.completions.get(run_id, request_id)
