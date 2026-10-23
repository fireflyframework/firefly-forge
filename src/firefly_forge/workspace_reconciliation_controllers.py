# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit review, one environment transition and exact native receipt readback."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, QueryParam, get_mapping, post_mapping, request_mapping

from firefly_forge.source_budget_allowances import (
    SourceBudgetAllowanceInput,
    SourceBudgetProposal,
    SourceBudgetReadmissions,
)
from firefly_forge.source_edit_models import SourceEditContinuationInput
from firefly_forge.source_edit_observed_reconciliation import (
    ObservedSourceReadmissionInput,
    ObservedSourceReconciliation,
)
from firefly_forge.source_human_readmission import SourceHumanReadmissionInput, SourceHumanReadmissions
from firefly_forge.source_index_sanitation import IndexSanitationInput
from firefly_forge.source_result_readmission import SourceCompletedOutputInput, SourceCompletedOutputReadmissions
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


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/source-edit-continuations")
class SourceEditContinuationController:
    def __init__(self, reconciliations: WorkspaceReconciliations) -> None:
        self.reconciliations = reconciliations

    @get_mapping("/{request_id}/budget-allowance-candidate")
    async def budget_candidate(
        self,
        run_id: PathVar[str],
        request_id: PathVar[str],
        previous_catalog_path: QueryParam[str],
        read_file_lifetime_limit: QueryParam[int],
        activation_ceiling: QueryParam[int],
        additional_activations: QueryParam[int],
    ) -> dict[str, Any]:
        proposal = SourceBudgetProposal(
            version=1,
            read_file_lifetime_limit=read_file_lifetime_limit,
            activation_ceiling=activation_ceiling,
            additional_activations=additional_activations,
        )
        return await SourceBudgetReadmissions(self.reconciliations).candidate(
            run_id, request_id, previous_catalog_path, proposal
        )

    @post_mapping("/{request_id}/budget-allowances", status_code=201)
    async def admit_budget(
        self, run_id: PathVar[str], request_id: PathVar[str], body: Body[SourceBudgetAllowanceInput]
    ) -> dict[str, Any]:
        return await SourceBudgetReadmissions(self.reconciliations).admit(run_id, request_id, body)

    @get_mapping("/{request_id}/budget-allowances/{admission_id}")
    async def budget_receipt(
        self, run_id: PathVar[str], request_id: PathVar[str], admission_id: PathVar[str]
    ) -> dict[str, Any]:
        return await SourceBudgetReadmissions(self.reconciliations).get(run_id, request_id, admission_id)

    @get_mapping("/candidate")
    async def candidate(self, run_id: PathVar[str]) -> dict[str, Any]:
        return await self.reconciliations.source_candidate(run_id)

    @post_mapping("", status_code=201)
    async def admit(self, run_id: PathVar[str], body: Body[SourceEditContinuationInput]) -> dict[str, Any]:
        return await self.reconciliations.activate_source_edit(run_id, body)

    @get_mapping("/{request_id}/human-readmission-candidate")
    async def human_candidate(
        self, run_id: PathVar[str], request_id: PathVar[str], previous_catalog_path: QueryParam[str]
    ) -> dict[str, Any]:
        return await SourceHumanReadmissions(self.reconciliations).candidate(run_id, request_id, previous_catalog_path)

    @post_mapping("/{request_id}/human-readmissions", status_code=201)
    async def readmit_human(
        self, run_id: PathVar[str], request_id: PathVar[str], body: Body[SourceHumanReadmissionInput]
    ) -> dict[str, Any]:
        return await SourceHumanReadmissions(self.reconciliations).admit(run_id, request_id, body)

    @get_mapping("/{request_id}/human-readmissions/{admission_id}")
    async def human_receipt(
        self, run_id: PathVar[str], request_id: PathVar[str], admission_id: PathVar[str]
    ) -> dict[str, Any]:
        return await SourceHumanReadmissions(self.reconciliations).get(run_id, request_id, admission_id)

    @get_mapping("/{request_id}/completed-output-candidate")
    async def completed_output_candidate(
        self, run_id: PathVar[str], request_id: PathVar[str], previous_catalog_path: QueryParam[str]
    ) -> dict[str, Any]:
        return await SourceCompletedOutputReadmissions(self.reconciliations).candidate(
            run_id, request_id, previous_catalog_path
        )

    @post_mapping("/{request_id}/completed-output-readmissions", status_code=201)
    async def readmit_completed_output(
        self, run_id: PathVar[str], request_id: PathVar[str], body: Body[SourceCompletedOutputInput]
    ) -> dict[str, Any]:
        return await SourceCompletedOutputReadmissions(self.reconciliations).admit(run_id, request_id, body)

    @get_mapping("/{request_id}/completed-output-readmissions/{admission_id}")
    async def completed_output_receipt(
        self, run_id: PathVar[str], request_id: PathVar[str], admission_id: PathVar[str]
    ) -> dict[str, Any]:
        return await SourceCompletedOutputReadmissions(self.reconciliations).get(run_id, request_id, admission_id)

    @get_mapping("/{request_id}/index-sanitation-candidate")
    async def index_candidate(
        self, run_id: PathVar[str], request_id: PathVar[str], previous_catalog_path: QueryParam[str]
    ) -> dict[str, Any]:
        return await ObservedSourceReconciliation(self.reconciliations).index_sanitation_candidate(
            run_id, request_id, previous_catalog_path
        )

    @post_mapping("/{request_id}/index-sanitations", status_code=201)
    async def sanitize_index(
        self, run_id: PathVar[str], request_id: PathVar[str], body: Body[IndexSanitationInput]
    ) -> dict[str, Any]:
        return await ObservedSourceReconciliation(self.reconciliations).sanitize_index(run_id, request_id, body)

    @get_mapping("/{request_id}/index-sanitations/{sanitation_id}")
    async def index_receipt(
        self, run_id: PathVar[str], request_id: PathVar[str], sanitation_id: PathVar[str]
    ) -> dict[str, Any]:
        return await ObservedSourceReconciliation(self.reconciliations).index_sanitation_get(
            run_id, request_id, sanitation_id
        )

    @get_mapping("/{request_id}/observed-effect-candidate")
    async def observed_candidate(
        self, run_id: PathVar[str], request_id: PathVar[str], previous_catalog_path: QueryParam[str]
    ) -> dict[str, Any]:
        return await ObservedSourceReconciliation(self.reconciliations).candidate(
            run_id, request_id, previous_catalog_path
        )

    @post_mapping("/{request_id}/observed-effect-readmissions", status_code=201)
    async def readmit_observed(
        self, run_id: PathVar[str], request_id: PathVar[str], body: Body[ObservedSourceReadmissionInput]
    ) -> dict[str, Any]:
        return await ObservedSourceReconciliation(self.reconciliations).admit(run_id, request_id, body)

    @get_mapping("/{request_id}")
    async def receipt(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        value = await self.reconciliations.get(run_id, request_id)
        SourceEditContinuationInput.model_validate(value["request"])
        return value
