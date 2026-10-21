# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit review, one environment transition and exact native receipt readback."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping

from firefly_forge.workspace_reconciliation_models import WorkspaceReconciliationInput
from firefly_forge.workspace_reconciliations import WorkspaceReconciliations


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/workspace-reconciliations")
class WorkspaceReconciliationController:
    def __init__(self, reconciliations: WorkspaceReconciliations) -> None:
        self.reconciliations = reconciliations

    @get_mapping("/candidate")
    async def candidate(self, run_id: PathVar[str]) -> dict[str, Any]:
        return await self.reconciliations.candidate(run_id)

    @post_mapping("", status_code=201)
    async def admit(self, run_id: PathVar[str], body: Body[WorkspaceReconciliationInput]) -> dict[str, Any]:
        return await self.reconciliations.admit(run_id, body)

    @get_mapping("/{request_id}")
    async def receipt(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.reconciliations.get(run_id, request_id)
