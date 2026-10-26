# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Review and restore the original provider selection after a local handoff failure."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping

from firefly_forge.original_selection_models import OriginalSelectionReadmissionInput
from firefly_forge.original_selection_readmissions import OriginalSelectionReadmissions


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/original-selection-readmissions")
class OriginalSelectionReadmissionController:
    def __init__(self, readmissions: OriginalSelectionReadmissions) -> None:
        self.readmissions = readmissions

    @get_mapping("/candidate")
    async def candidate(self, run_id: PathVar[str]) -> dict[str, Any]:
        return await self.readmissions.candidate(run_id)

    @post_mapping("/{request_id}", status_code=201)
    async def request(
        self, run_id: PathVar[str], request_id: PathVar[str], body: Body[OriginalSelectionReadmissionInput]
    ) -> dict[str, Any]:
        return await self.readmissions.request(run_id, request_id, body)

    @get_mapping("/{request_id}")
    async def get(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.readmissions.get(run_id, request_id)
