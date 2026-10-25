# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native operator review of unknown command custody; no execution endpoint."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, QueryParam, get_mapping, post_mapping, request_mapping

from firefly_forge.unknown_command_acknowledgements import UnknownCommandAcknowledgements
from firefly_forge.unknown_command_models import UnknownCommandAcknowledgementV1


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/sandbox-executions/{execution_id}/unknown-command-acknowledgements")
class UnknownCommandAcknowledgementController:
    def __init__(self, acknowledgements: UnknownCommandAcknowledgements) -> None:
        self.acknowledgements = acknowledgements

    @get_mapping("/candidate")
    async def candidate(
        self,
        run_id: PathVar[str],
        execution_id: PathVar[str],
        agent: QueryParam[str],
        adoption_request_id: QueryParam[str],
    ) -> dict[str, Any]:
        return await self.acknowledgements.candidate(
            run_id, execution_id, agent=agent, adoption_request_id=adoption_request_id
        )

    @post_mapping("/{request_id}", status_code=201)
    async def request(
        self,
        run_id: PathVar[str],
        execution_id: PathVar[str],
        request_id: PathVar[str],
        body: Body[UnknownCommandAcknowledgementV1],
    ) -> dict[str, Any]:
        return await self.acknowledgements.request(run_id, execution_id, request_id, body)

    @get_mapping("/{request_id}")
    async def get(self, run_id: PathVar[str], execution_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.acknowledgements.get(run_id, execution_id, request_id)
