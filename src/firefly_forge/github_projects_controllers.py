# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Typed authenticated board setup and owner-queued synchronization routes."""

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, put_mapping, request_mapping

from firefly_forge.github_projects import GitHubProjects
from firefly_forge.github_projects_models import (
    BoardConnectionInputV1,
    BoardConnectionViewV1,
    BoardCreateInputV1,
    BoardCreationReceiptV1,
    BoardDiscoveryInputV1,
    BoardDiscoveryV1,
    BoardObserveInputV1,
    BoardRequestReceiptV1,
    BoardSyncInputV1,
)
from firefly_forge.github_projects_requests import GitHubProjectsRequests
from firefly_forge.github_projects_worker import GitHubProjectsWorker
from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied


@rest_controller
@request_mapping("/api/v1/projects/{project_id}/repositories/{repository_id}/github-projects")
class GitHubProjectsController:
    def __init__(self, manager: GitHubProjects, tickets: GitHubProjectsRequests, worker: GitHubProjectsWorker) -> None:
        self.manager, self.tickets, self.worker = manager, tickets, worker

    @staticmethod
    def actor() -> Principal:
        actor = CURRENT_PRINCIPAL.get()
        if actor is None:
            raise AccessDenied("Sign in to inspect or configure a project board")
        return actor

    @get_mapping("/connection")
    async def connection(self, project_id: PathVar[str], repository_id: PathVar[str]) -> BoardConnectionViewV1:
        return await self.manager.view(project_id, repository_id, self.actor())

    @post_mapping("/creations")
    async def create(
        self, project_id: PathVar[str], repository_id: PathVar[str], body: Body[BoardCreateInputV1]
    ) -> BoardCreationReceiptV1:
        return await self.manager.create(project_id, repository_id, body, self.actor())

    @get_mapping("/creations/{request_id}")
    async def creation_receipt(
        self, project_id: PathVar[str], repository_id: PathVar[str], request_id: PathVar[str]
    ) -> BoardCreationReceiptV1:
        return await self.manager.creation_result(project_id, repository_id, request_id, self.actor())

    @post_mapping("/discover")
    async def discover(
        self, project_id: PathVar[str], repository_id: PathVar[str], body: Body[BoardDiscoveryInputV1]
    ) -> BoardDiscoveryV1:
        return await self.manager.discover(project_id, repository_id, body, self.actor())

    @put_mapping("/connection")
    async def configure(
        self, project_id: PathVar[str], repository_id: PathVar[str], body: Body[BoardConnectionInputV1]
    ) -> BoardConnectionViewV1:
        return await self.manager.configure(project_id, repository_id, body, self.actor())

    @post_mapping("/runs/{run_id}/sync", status_code=202)
    async def sync(
        self, project_id: PathVar[str], repository_id: PathVar[str], run_id: PathVar[str], body: Body[BoardSyncInputV1]
    ) -> BoardRequestReceiptV1:
        actor = self.actor()
        await self.tickets.admit(project_id, repository_id, run_id, body, actor)
        return BoardRequestReceiptV1.model_validate(
            await self.tickets.get(project_id, repository_id, body.request_id, actor)
        )

    @get_mapping("/requests/{request_id}")
    async def receipt(
        self, project_id: PathVar[str], repository_id: PathVar[str], request_id: PathVar[str]
    ) -> BoardRequestReceiptV1:
        return BoardRequestReceiptV1.model_validate(
            await self.tickets.get(project_id, repository_id, request_id, self.actor())
        )

    @post_mapping("/requests/{request_id}/observe")
    async def observe(
        self,
        project_id: PathVar[str],
        repository_id: PathVar[str],
        request_id: PathVar[str],
        body: Body[BoardObserveInputV1],
    ) -> BoardRequestReceiptV1:
        await self.worker.observe(project_id, repository_id, request_id, body, self.actor())
        return BoardRequestReceiptV1.model_validate(
            await self.tickets.get(project_id, repository_id, request_id, self.actor())
        )
