# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Finite native setup for one maintain child's automatic GitHub communication."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping

from firefly_forge.collaboration import Collaboration
from firefly_forge.scm_maintain_ticket_models import MaintainTicketInput
from firefly_forge.scm_maintain_tickets import MaintainTickets


@rest_controller
@request_mapping("/api/v1/projects/{project_id}/repositories/{repository_id}/maintain-ticket-bindings")
class MaintainTicketController:
    def __init__(self, collaboration: Collaboration) -> None:
        self.tickets = MaintainTickets(collaboration)

    @post_mapping("", status_code=201)
    async def configure(
        self, project_id: PathVar[str], repository_id: PathVar[str], body: Body[MaintainTicketInput]
    ) -> dict[str, Any]:
        return await self.tickets.configure(project_id, repository_id, body)

    @get_mapping("/requests/{request_id}")
    async def read(
        self, project_id: PathVar[str], repository_id: PathVar[str], request_id: PathVar[str]
    ) -> dict[str, Any]:
        return await self.tickets.read(project_id, repository_id, request_id)
