# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Project-authorized human read routes; remote scopes come only from saved configuration."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, put_mapping, request_mapping

from firefly_forge.analytics import ProjectAnalytics
from firefly_forge.analytics_models import AnalyticsConfigurationInput
from firefly_forge.identity import Principal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied


@rest_controller
@request_mapping("/api/v1/projects/{project}/analytics")
class ProjectAnalyticsController:
    def __init__(self, analytics: ProjectAnalytics) -> None:
        self.analytics = analytics

    @staticmethod
    def _actor() -> Principal:
        actor = CURRENT_PRINCIPAL.get()
        if actor is None:
            raise AccessDenied("An authenticated project reader is required")
        return actor

    @get_mapping("/configuration")
    async def configuration(self, project: PathVar[str]) -> dict[str, Any]:
        return await self.analytics.configuration(project, self._actor())

    @put_mapping("/configuration")
    async def configure(self, project: PathVar[str], body: Body[AnalyticsConfigurationInput]) -> dict[str, Any]:
        return await self.analytics.configure(project, body, self._actor())

    @get_mapping("/catalog")
    async def catalog(self, project: PathVar[str]) -> dict[str, Any]:
        return await self.analytics.catalog(project, self._actor())

    @get_mapping("/tables/{table}")
    async def table(self, project: PathVar[str], table: PathVar[str]) -> dict[str, Any]:
        return await self.analytics.table(project, table, self._actor())

    @get_mapping("/tables/{table}/objects/{object_id}")
    async def schema_object(
        self, project: PathVar[str], table: PathVar[str], object_id: PathVar[str]
    ) -> dict[str, Any]:
        return await self.analytics.schema_object(project, table, object_id, self._actor())

    @get_mapping("/prepared/{query}")
    async def prepared(self, project: PathVar[str], query: PathVar[str]) -> dict[str, Any]:
        return await self.analytics.prepared(project, query, self._actor())
