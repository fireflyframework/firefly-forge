# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Polling configuration accepts references to server observations, never trusted observations."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, QueryParam, get_mapping, post_mapping, request_mapping

from firefly_forge.scm_poll_models import PollConfigurationV1, SCMPollActorBindingInputV2
from firefly_forge.scm_polling import SCMPolling


@rest_controller
@request_mapping("/api/v1/projects/{project_id}/repositories/{repository_id}/scm-polling")
class SCMPollController:
    def __init__(self, polling: SCMPolling) -> None:
        self.polling = polling

    @get_mapping("/setup")
    async def setup(self, project_id: PathVar[str], repository_id: PathVar[str]) -> dict[str, Any]:
        return await self.polling.setup(project_id, repository_id)

    @post_mapping("/configuration", status_code=201)
    async def configure(
        self, project_id: PathVar[str], repository_id: PathVar[str], body: Body[PollConfigurationV1]
    ) -> dict[str, Any]:
        return await self.polling.configure(project_id, repository_id, body)

    @get_mapping("/configuration/requests/{request_id}")
    async def configuration(
        self, project_id: PathVar[str], repository_id: PathVar[str], request_id: PathVar[str]
    ) -> dict[str, Any]:
        return await self.polling.configuration(project_id, repository_id, request_id)

    @get_mapping("/status")
    async def status(self, project_id: PathVar[str], repository_id: PathVar[str]) -> dict[str, Any]:
        return await self.polling.status(project_id, repository_id)

    @get_mapping("/candidates")
    async def candidates(self, project_id: PathVar[str], repository_id: PathVar[str]) -> dict[str, Any]:
        return await self.polling.candidates(project_id, repository_id)

    @post_mapping("/bindings", status_code=201)
    async def configure_binding(
        self, project_id: PathVar[str], repository_id: PathVar[str], body: Body[SCMPollActorBindingInputV2]
    ) -> dict[str, Any]:
        return await self.polling.configure_binding(project_id, repository_id, body)

    @get_mapping("/bindings")
    async def heads(
        self, project_id: PathVar[str], repository_id: PathVar[str], after: QueryParam[str | None] = None
    ) -> dict[str, Any]:
        return await self.polling.heads(project_id, repository_id, after)

    @get_mapping("/bindings/requests/{request_id}")
    async def exact(
        self, project_id: PathVar[str], repository_id: PathVar[str], request_id: PathVar[str]
    ) -> dict[str, Any]:
        return await self.polling.binding(project_id, repository_id, request_id)
