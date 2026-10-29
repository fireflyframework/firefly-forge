# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native operations for immutable official framework references."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, QueryParam, get_mapping, post_mapping, request_mapping

from firefly_forge.framework_knowledge import FrameworkKnowledge
from firefly_forge.framework_knowledge_models import (
    FrameworkBudgetRenewalInput,
    FrameworkCheckUpdatesInput,
    FrameworkGenerationTargetInput,
    FrameworkRefreshCancelInput,
    FrameworkRefreshInput,
    FrameworkRefreshResumeInput,
    FrameworkSearchInput,
)


@rest_controller
@request_mapping("/api/v1/knowledge/frameworks")
class FrameworkKnowledgeController:
    def __init__(self, frameworks: FrameworkKnowledge) -> None:
        self.frameworks = frameworks

    @get_mapping("/status")
    async def status(self) -> dict[str, Any]:
        return await self.frameworks.status()

    @post_mapping("/{framework}/check-updates")
    async def check_updates(self, framework: PathVar[str], body: Body[FrameworkCheckUpdatesInput]) -> dict[str, Any]:
        return await self.frameworks.check_updates(framework, body)

    @post_mapping("/{framework}/refreshes/{request_id}", status_code=201)
    async def refresh(
        self, framework: PathVar[str], request_id: PathVar[str], body: Body[FrameworkRefreshInput]
    ) -> dict[str, Any]:
        return await self.frameworks.refresh(framework, request_id, body)

    @get_mapping("/{framework}/refreshes/{request_id}")
    async def get_refresh(self, framework: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.frameworks.get_refresh(framework, request_id)

    @post_mapping("/{framework}/refreshes/{request_id}/observe")
    async def observe(self, framework: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.frameworks.observe(framework, request_id)

    @post_mapping("/{framework}/refreshes/{request_id}/budget")
    async def renew_budget(
        self, framework: PathVar[str], request_id: PathVar[str], body: Body[FrameworkBudgetRenewalInput]
    ) -> dict[str, Any]:
        return await self.frameworks.renew_budget(framework, request_id, body)

    @post_mapping("/{framework}/refreshes/{request_id}/cancel")
    async def cancel(
        self, framework: PathVar[str], request_id: PathVar[str], body: Body[FrameworkRefreshCancelInput]
    ) -> dict[str, Any]:
        return await self.frameworks.cancel(framework, request_id, body)

    @post_mapping("/{framework}/refreshes/{request_id}/resume")
    async def resume(
        self, framework: PathVar[str], request_id: PathVar[str], body: Body[FrameworkRefreshResumeInput]
    ) -> dict[str, Any]:
        return await self.frameworks.resume(framework, request_id, body)

    @get_mapping("/{framework}/corpora")
    async def corpora(
        self, framework: PathVar[str], cursor: QueryParam[str | None] = None, page_size: QueryParam[int] = 50
    ) -> dict[str, Any]:
        return await self.frameworks.corpora(framework, cursor=cursor, page_size=page_size)

    @get_mapping("/{framework}/generation-candidates")
    async def generation_candidates(
        self,
        framework: PathVar[str],
        corpus_ref: QueryParam[str],
        cursor: QueryParam[str | None] = None,
        page_size: QueryParam[int] = 50,
    ) -> dict[str, Any]:
        return await self.frameworks.generation_candidates(framework, corpus_ref, cursor=cursor, page_size=page_size)

    @post_mapping("/{framework}/generation-targets/{request_id}", status_code=201)
    async def activate_generation_target(
        self, framework: PathVar[str], request_id: PathVar[str], body: Body[FrameworkGenerationTargetInput]
    ) -> dict[str, Any]:
        return await self.frameworks.activate_generation_target(framework, request_id, body)

    @get_mapping("/{framework}/generation-targets/{request_id}")
    async def generation_target(self, framework: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.frameworks.generation_target(framework, request_id)

    @get_mapping("/{framework}/corpora/{corpus_ref}/sources")
    async def sources(
        self,
        framework: PathVar[str],
        corpus_ref: PathVar[str],
        cursor: QueryParam[str | None] = None,
        page_size: QueryParam[int] = 50,
    ) -> dict[str, Any]:
        return await self.frameworks.sources(framework, corpus_ref, cursor=cursor, page_size=page_size)

    @post_mapping("/{framework}/search")
    async def search(self, framework: PathVar[str], body: Body[FrameworkSearchInput]) -> dict[str, Any]:
        return await self.frameworks.search(framework, body)
