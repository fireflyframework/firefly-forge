# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Product-scoped source inspection and evidence review without executing project code."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping

from firefly_forge.analysis_context import AnalysisContextInput, ReviewedAnalysisContext
from firefly_forge.reverse_engineering import AnalysisInput, AnalysisReview, ReverseEngineering


@rest_controller
@request_mapping("/api/v1/projects/{project_id}/analyses")
class AnalysisController:
    def __init__(self, analysis: ReverseEngineering, context: ReviewedAnalysisContext) -> None:
        self.manager = analysis
        self.context = context

    @get_mapping("")
    async def analyses(self, project_id: PathVar[str]) -> list[dict[str, Any]]:
        return await self.manager.analyses(project_id)

    @post_mapping("", status_code=201)
    async def analyze(self, project_id: PathVar[str], body: Body[AnalysisInput]) -> dict[str, Any]:
        return await self.manager.analyze(project_id, body)

    @get_mapping("/{analysis_id}")
    async def analysis(self, project_id: PathVar[str], analysis_id: PathVar[str]) -> dict[str, Any]:
        return await self.manager.analysis(project_id, analysis_id)

    @post_mapping("/{analysis_id}/review")
    async def review(
        self, project_id: PathVar[str], analysis_id: PathVar[str], body: Body[AnalysisReview]
    ) -> dict[str, Any]:
        return await self.manager.review(project_id, analysis_id, body)

    @post_mapping("/{analysis_id}/knowledge", status_code=202)
    async def admit_knowledge(
        self, project_id: PathVar[str], analysis_id: PathVar[str], body: Body[AnalysisContextInput]
    ) -> dict[str, Any]:
        return await self.context.admit(project_id, analysis_id, body)
