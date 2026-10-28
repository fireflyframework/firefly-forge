# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Typed Python client for factory operators and agent integrations."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self
from uuid import UUID

import httpx
from pydantic import BaseModel

from firefly_forge.models import RepositoryInput, RunInput

if TYPE_CHECKING:
    from firefly_forge.access_controllers import MembershipInput
    from firefly_forge.agent_checkpoint_models import RestoreInput
    from firefly_forge.agent_roles import ProjectRoles, RoleDocument, WorkerResponse
    from firefly_forge.analytics_models import AnalyticsConfigurationInput
    from firefly_forge.assistant_attachments import AttachmentInput
    from firefly_forge.campaign_delivery_plan_models import CampaignDeliveryPlanInput
    from firefly_forge.campaign_execution_budget import CampaignBudgetRenewalV1, CampaignExecutionPolicyInputV1
    from firefly_forge.campaign_report_history import CampaignReportArchiveInput
    from firefly_forge.checkpoint_continuation_models import CheckpointContinuationInputV1
    from firefly_forge.checkpoint_mode_models import CheckpointModeAdoptionInput
    from firefly_forge.coding_check_models import CodingCheckGrantInput, CodingCheckRevocationInput
    from firefly_forge.framework_knowledge_models import (
        FrameworkBudgetRenewalInput,
        FrameworkCheckUpdatesInput,
        FrameworkGenerationTargetInput,
        FrameworkRefreshCancelInput,
        FrameworkRefreshInput,
        FrameworkRefreshResumeInput,
        FrameworkSearchInput,
    )
    from firefly_forge.git_delivery_models import GitDeliveryInputV1, GitDeliveryPolicyInputV1
    from firefly_forge.git_diff_models import GitDiffIndexPageV1, GitDiffPageV1
    from firefly_forge.invalid_read_continuity_models import InvalidReadAdmissionInputV1, InvalidReadAdmissionInputV2
    from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1
    from firefly_forge.invalid_read_recovery_models import InvalidReadRecoveryInputV1
    from firefly_forge.model_profiles import ModelProfileInput
    from firefly_forge.original_selection_models import OriginalSelectionReadmissionInput
    from firefly_forge.product_assistant import AssistantRecoveryInput, AssistantTurnInput
    from firefly_forge.product_lifecycle import FeatureChangeInput
    from firefly_forge.project_campaigns import CampaignAction, CampaignLaunch, CampaignRetry
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.provider_recovery_allowances import ProviderRecoveryAllowanceInput
    from firefly_forge.provider_rejection_models import RejectedBoundaryRecoveryInputV1, ReviewedProviderRejectionV1
    from firefly_forge.reverse_engineering import AnalysisReview
    from firefly_forge.run_creation_models import RunCreationCancelInput, RunCreationInput
    from firefly_forge.runner_continuations import RunnerContinuationInput
    from firefly_forge.sandbox_models import SandboxInput
    from firefly_forge.scm_authority_models import SCMActorBindingInputV1
    from firefly_forge.scm_poll_functional_models import SCMPollFunctionalGrantInputV1
    from firefly_forge.scm_poll_resume_models import SCMPollResumeGrantInputV3
    from firefly_forge.scm_product_change_models import SCMProductChangeGrantInputV1
    from firefly_forge.scm_product_intake_models import SCMProductIntakeConsentInputV1
    from firefly_forge.scm_resume_models import SCMResumeGrantInputV2
    from firefly_forge.scm_source_edit_models import SCMSourceEditGrantInputV1
    from firefly_forge.source_budget_allowances import (
        SourceBudgetAllowanceInput,
        SourceBudgetPolicy,
        SourceBudgetProposal,
    )
    from firefly_forge.source_edit_models import SourceEditContinuationInput
    from firefly_forge.task_routing import TaskRoutingInput
    from firefly_forge.task_routing_mcp import TaskRoutingInput as MCPTaskRoutingInput
    from firefly_forge.unknown_command_models import UnknownCommandAcknowledgementV1
    from firefly_forge.user_administration import (
        AdministrationReconciliation,
        AdministrationRecovery,
        UserInviteInput,
        UserProvisionInput,
    )
    from firefly_forge.worker_evidence import WorkerImage
    from firefly_forge.workspace_reconciliation_models import WorkspaceReconciliationInput


class ForgeClient:
    def __init__(self, base_url: str, token: str) -> None:
        if not token:
            raise ValueError("A factory operator token is required")
        self.http = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v1/", headers={"Authorization": f"Bearer {token}"}, timeout=60
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: Any) -> None:
        self.http.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.http.request(method, path, **kwargs)
        if response.is_error:
            try:
                detail = response.json().get("detail", response.reason_phrase)
            except ValueError:
                detail = response.reason_phrase
            raise RuntimeError(f"Factory request failed (HTTP {response.status_code}): {detail}")
        return response.json()

    def repositories(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "repositories"))

    def add_repository(self, request: RepositoryInput) -> dict[str, Any]:
        return dict(self.request("POST", "repositories", json=request.model_dump()))

    def create_run(self, request: RunInput) -> dict[str, Any]:
        return dict(self.request("POST", "runs", json=request.model_dump(exclude_unset=True)))

    @staticmethod
    def _recovery_identifier(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
            raise ValueError("Use an exact lowercase 32-character factory identifier")
        return value

    @staticmethod
    def _recovery_body(model: type[BaseModel], request: BaseModel | dict[str, Any]) -> dict[str, Any]:
        try:
            value = (
                request.model_dump(exclude_unset=True, warnings=False) if isinstance(request, BaseModel) else request
            )
            return model.model_validate(value).model_dump(mode="json", exclude_unset=True)
        except ValueError:
            raise ValueError("Use the original recovery request with its exact identities, scope and digests") from None

    def provider_recovery_allowance_candidate(
        self, run_id: str, agent: Literal["codex", "claude"], *, adopt_current_executable: bool = False
    ) -> dict[str, Any]:
        run_id = self._recovery_identifier(run_id)
        if agent not in {"codex", "claude"} or type(adopt_current_executable) is not bool:
            raise ValueError("Select an official provider and an explicit CLI adoption choice")
        return dict(
            self.request(
                "GET",
                f"runs/{run_id}/provider-recoveries/allowances/candidate",
                params={"agent": agent, "adopt_current_executable": str(adopt_current_executable).lower()},
            )
        )

    def reserve_provider_recovery_allowance(
        self, run_id: str, request: ProviderRecoveryAllowanceInput | dict[str, Any]
    ) -> dict[str, Any]:
        """Reserve once. After uncertain delivery, read the same receipt; never choose another identity."""
        from firefly_forge.provider_recovery_allowances import ProviderRecoveryAllowanceInput

        run_id = self._recovery_identifier(run_id)
        body = self._recovery_body(ProviderRecoveryAllowanceInput, request)
        return dict(self.request("POST", f"runs/{run_id}/provider-recoveries/allowances", json=body))

    def provider_recovery_allowance(self, run_id: str, request_id: str) -> dict[str, Any]:
        run_id, request_id = self._recovery_identifier(run_id), self._recovery_identifier(request_id)
        return dict(self.request("GET", f"runs/{run_id}/provider-recoveries/allowances/{request_id}"))

    def workspace_reconciliation_candidate(self, run_id: str) -> dict[str, Any]:
        return dict(
            self.request("GET", f"runs/{self._recovery_identifier(run_id)}/workspace-reconciliations/candidate")
        )

    def source_edit_candidate(self, run_id: str) -> dict[str, Any]:
        return dict(
            self.request("GET", f"runs/{self._recovery_identifier(run_id)}/source-edit-continuations/candidate")
        )

    def activate_source_edit(
        self, run_id: str, request: SourceEditContinuationInput | dict[str, Any]
    ) -> dict[str, Any]:
        """Submit once; unknown transport outcomes require the same request's readback."""
        from firefly_forge.source_edit_models import SourceEditContinuationInput

        body = self._recovery_body(SourceEditContinuationInput, request)
        return dict(
            self.request("POST", f"runs/{self._recovery_identifier(run_id)}/source-edit-continuations", json=body)
        )

    def source_edit_continuation(self, run_id: str, request_id: str) -> dict[str, Any]:
        run_id, request_id = self._recovery_identifier(run_id), self._recovery_identifier(request_id)
        return dict(self.request("GET", f"runs/{run_id}/source-edit-continuations/{request_id}"))

    def source_budget_policy(self) -> dict[str, Any]:
        return dict(self.request("GET", "source-budget/policy"))

    def configure_source_budget_policy(self, request: SourceBudgetPolicy | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.source_budget_allowances import SourceBudgetPolicy

        body = self._recovery_body(SourceBudgetPolicy, request)
        return dict(self.request("PUT", "source-budget/policy", json=body))

    def _source_budget_path(self, run_id: str, parent_request_id: str) -> str:
        run_id = self._recovery_identifier(run_id)
        parent_request_id = self._recovery_identifier(parent_request_id)
        return f"runs/{run_id}/source-edit-continuations/{parent_request_id}"

    def source_budget_candidate(
        self,
        run_id: str,
        parent_request_id: str,
        previous_catalog_path: str,
        proposal: SourceBudgetProposal | dict[str, Any],
    ) -> dict[str, Any]:
        from firefly_forge.source_budget_allowances import SourceBudgetProposal

        path = self._source_budget_path(run_id, parent_request_id)
        if (
            not isinstance(previous_catalog_path, str)
            or not previous_catalog_path.strip()
            or len(previous_catalog_path) > 4096
            or "\x00" in previous_catalog_path
        ):
            raise ValueError("Select the original bounded native model catalog path")
        body = self._recovery_body(SourceBudgetProposal, proposal)
        params = {"previous_catalog_path": previous_catalog_path, **{k: v for k, v in body.items() if k != "version"}}
        return dict(self.request("GET", path + "/budget-allowance-candidate", params=params))

    def admit_source_budget(
        self, run_id: str, parent_request_id: str, request: SourceBudgetAllowanceInput | dict[str, Any]
    ) -> dict[str, Any]:
        """Submit once; an uncertain transport requires reading this same request's receipt."""
        from firefly_forge.source_budget_allowances import SourceBudgetAllowanceInput

        path = self._source_budget_path(run_id, parent_request_id)
        body = self._recovery_body(SourceBudgetAllowanceInput, request)
        return dict(self.request("POST", path + "/budget-allowances", json=body))

    def source_budget_receipt(self, run_id: str, parent_request_id: str, request_id: str) -> dict[str, Any]:
        path = self._source_budget_path(run_id, parent_request_id)
        request_id = self._recovery_identifier(request_id)
        return dict(self.request("GET", path + "/budget-allowances/" + request_id))

    def reconcile_workspace(
        self, run_id: str, request: WorkspaceReconciliationInput | dict[str, Any]
    ) -> dict[str, Any]:
        """Submit the reviewed environment transition once; uncertain delivery requires exact readback."""
        from firefly_forge.workspace_reconciliation_models import WorkspaceReconciliationInput

        run_id = self._recovery_identifier(run_id)
        body = self._recovery_body(WorkspaceReconciliationInput, request)
        return dict(self.request("POST", f"runs/{run_id}/workspace-reconciliations", json=body))

    def workspace_reconciliation(self, run_id: str, request_id: str) -> dict[str, Any]:
        run_id, request_id = self._recovery_identifier(run_id), self._recovery_identifier(request_id)
        return dict(self.request("GET", f"runs/{run_id}/workspace-reconciliations/{request_id}"))

    def coding_check_candidate(self, run_id: str, recovery_request_id: str, *, bind_port: int = 0) -> dict[str, Any]:
        run_id = self._recovery_identifier(run_id)
        recovery_request_id = self._recovery_identifier(recovery_request_id)
        if type(bind_port) is not int or not (bind_port == 0 or 1024 <= bind_port <= 65535):
            raise ValueError("Use the explicitly reviewed unprivileged loopback port or zero")
        return dict(
            self.request(
                "GET",
                f"runs/{run_id}/coding-check-grants/candidate",
                params={
                    "recovery_request_id": recovery_request_id,
                    "bind_port": bind_port,
                },
            )
        )

    def grant_coding_checks(self, run_id: str, request: CodingCheckGrantInput | dict[str, Any]) -> dict[str, Any]:
        """Submit the reviewed original grant once; uncertain delivery requires exact receipt readback."""
        from firefly_forge.coding_check_models import CodingCheckGrantInput

        run_id = self._recovery_identifier(run_id)
        return dict(
            self.request(
                "POST", f"runs/{run_id}/coding-check-grants", json=self._recovery_body(CodingCheckGrantInput, request)
            )
        )

    def coding_check_receipt(self, run_id: str, request_id: str) -> dict[str, Any]:
        run_id, request_id = self._recovery_identifier(run_id), self._recovery_identifier(request_id)
        return dict(self.request("GET", f"runs/{run_id}/coding-check-grants/{request_id}"))

    def coding_check_current(self, run_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"runs/{self._recovery_identifier(run_id)}/coding-check-grants/current"))

    def revoke_coding_checks(self, run_id: str, request: CodingCheckRevocationInput | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.coding_check_models import CodingCheckRevocationInput

        run_id = self._recovery_identifier(run_id)
        return dict(
            self.request(
                "POST",
                f"runs/{run_id}/coding-check-grants/revocations",
                json=self._recovery_body(CodingCheckRevocationInput, request),
            )
        )

    def request_run_creation(self, request_id: str, request: RunCreationInput | dict[str, Any]) -> dict[str, Any]:
        """Submit one retained version-1 envelope; uncertain delivery never selects a follow-up action."""
        from firefly_forge.run_creation_models import RunCreationInput

        path = f"run-creation-requests/{self._recovery_identifier(request_id)}"
        body = self._recovery_body(RunCreationInput, request)
        return dict(self.request("POST", path, json=body))

    def run_creation_request(self, request_id: str) -> dict[str, Any]:
        """Read the original admission receipt without replaying a missing request."""
        return dict(self.request("GET", f"run-creation-requests/{self._recovery_identifier(request_id)}"))

    def cancel_run_creation_request(
        self, request_id: str, request: RunCreationCancelInput | dict[str, Any]
    ) -> dict[str, Any]:
        """Explicitly settle the original input digest and scope; this does not cancel an admitted run."""
        from firefly_forge.run_creation_models import RunCreationCancelInput

        path = f"run-creation-requests/{self._recovery_identifier(request_id)}/cancel"
        body = self._recovery_body(RunCreationCancelInput, request)
        return dict(self.request("POST", path, json=body))

    def run_creation_decision(self, request_id: str) -> dict[str, Any]:
        """Read an admitted/cancelled decision; a missing decision remains unresolved."""
        return dict(self.request("GET", f"run-creation-requests/{self._recovery_identifier(request_id)}/decision"))

    def agent_checkpoint_candidates(self, identifier: str) -> dict[str, Any]:
        """Inspect retained original-result references without requesting continuation or consumption."""
        return dict(self.request("GET", f"runs/{self._recovery_identifier(identifier)}/agent-checkpoints/candidates"))

    def _agent_checkpoint_restore_path(self, identifier: str, completion_id: str) -> str:
        if not isinstance(completion_id, str) or not re.fullmatch(r"fbr1_[a-f0-9]{32}", completion_id):
            raise ValueError("Use the exact original completion request identifier")
        return f"runs/{self._recovery_identifier(identifier)}/agent-checkpoints/{completion_id}/restores"

    def request_agent_checkpoint_restore(
        self, identifier: str, completion_id: str, request: RestoreInput | dict[str, Any]
    ) -> dict[str, Any]:
        """Admit only the caller's retained restore request and explicitly selected original result."""
        from firefly_forge.agent_checkpoint_models import RestoreInput

        path = self._agent_checkpoint_restore_path(identifier, completion_id)
        body = self._recovery_body(RestoreInput, request)
        return dict(self.request("POST", path, json=body))

    def agent_checkpoint_restore(self, identifier: str, completion_id: str, request_id: str) -> dict[str, Any]:
        """Read the same restore ticket without retrying admission or consuming a result."""
        path = self._agent_checkpoint_restore_path(identifier, completion_id)
        return dict(self.request("GET", path + "/" + self._recovery_identifier(request_id)))

    def invalid_read_continuity_candidate(self, identifier: str) -> dict[str, Any]:
        """Inspect a held same-attempt candidate without admitting or dispatching it."""
        return dict(
            self.request("GET", f"runs/{self._recovery_identifier(identifier)}/invalid-read-continuity/candidate")
        )

    def request_invalid_read_continuity(
        self,
        identifier: str,
        request_id: str,
        request: InvalidReadAdmissionInputV1 | InvalidReadAdmissionInputV2 | dict[str, Any],
    ) -> dict[str, Any]:
        """Submit once; unavailable observations require the explicit version-2 acknowledgement."""
        from firefly_forge.invalid_read_continuity_models import ADMISSION

        body = ADMISSION.validate_python(request.model_dump() if isinstance(request, BaseModel) else request)
        namespace = "requests" if body.version == 1 else "requests-v2"
        base = f"runs/{self._recovery_identifier(identifier)}/invalid-read-continuity"
        path = f"{base}/{namespace}/{self._recovery_identifier(request_id)}"
        return dict(self.request("POST", path, json=body.model_dump(mode="json")))

    def invalid_read_continuity(self, identifier: str, request_id: str) -> dict[str, Any]:
        """Read the original one-use ticket after a lost response; never repeat admission."""
        base = f"runs/{self._recovery_identifier(identifier)}/invalid-read-continuity"
        path = f"{base}/requests/{self._recovery_identifier(request_id)}"
        return dict(self.request("GET", path))

    def _invalid_read_recovery_path(self, identifier: str, request_id: str) -> str:
        return (
            f"runs/{self._recovery_identifier(identifier)}/invalid-read-continuity/requests/"
            + self._recovery_identifier(request_id)
        )

    def invalid_read_recovery_candidate(self, identifier: str, request_id: str) -> dict[str, Any]:
        """Inspect whether the exact original request failed before any worker claim."""
        return dict(
            self.request("GET", self._invalid_read_recovery_path(identifier, request_id) + "/recovery-candidate")
        )

    def request_invalid_read_recovery(
        self,
        identifier: str,
        request_id: str,
        recovery_id: str,
        request: InvalidReadRecoveryInputV1 | dict[str, Any],
    ) -> dict[str, Any]:
        """Submit one explicit lease recovery action; retain its ID for readback."""
        from firefly_forge.invalid_read_recovery_models import InvalidReadRecoveryInputV1

        path = (
            self._invalid_read_recovery_path(identifier, request_id)
            + "/recoveries/"
            + self._recovery_identifier(recovery_id)
        )
        body = self._recovery_body(InvalidReadRecoveryInputV1, request)
        return dict(self.request("POST", path, json=body))

    def invalid_read_recovery(self, identifier: str, request_id: str) -> dict[str, Any]:
        """Read the saved recovery witness without submitting another action."""
        return dict(self.request("GET", self._invalid_read_recovery_path(identifier, request_id) + "/recovery"))

    def rejected_boundary_recovery_candidate(
        self, identifier: str, request_id: str, evidence: ReviewedProviderRejectionV1 | dict[str, Any]
    ) -> dict[str, Any]:
        """Preview an administrator-reviewed exact provider rejection; this grants no dispatch."""
        from firefly_forge.provider_rejection_models import ReviewedProviderRejectionV1

        body = self._recovery_body(ReviewedProviderRejectionV1, evidence)
        path = self._invalid_read_recovery_path(identifier, request_id) + "/rejected-boundary-candidate"
        return dict(self.request("POST", path, json=body))

    def request_rejected_boundary_recovery(
        self,
        identifier: str,
        request_id: str,
        recovery_id: str,
        request: RejectedBoundaryRecoveryInputV1 | dict[str, Any],
    ) -> dict[str, Any]:
        """Admit once on the original ticket; retain the action ID for readback after a lost response."""
        from firefly_forge.provider_rejection_models import RejectedBoundaryRecoveryInputV1

        body = self._recovery_body(RejectedBoundaryRecoveryInputV1, request)
        path = (
            self._invalid_read_recovery_path(identifier, request_id)
            + "/rejected-boundary-recoveries/"
            + self._recovery_identifier(recovery_id)
        )
        return dict(self.request("POST", path, json=body))

    def rejected_boundary_recovery(self, identifier: str, request_id: str) -> dict[str, Any]:
        """Read the original rejected-boundary action without resubmitting it."""
        return dict(
            self.request(
                "GET", self._invalid_read_recovery_path(identifier, request_id) + "/rejected-boundary-recovery"
            )
        )

    def readonly_observation_recovery_candidate(self, identifier: str, request_id: str) -> dict[str, Any]:
        """Inspect a consumed read turn; this does not grant dispatch or retry its callback."""
        return dict(
            self.request(
                "GET", self._invalid_read_recovery_path(identifier, request_id) + "/readonly-observation-candidate"
            )
        )

    def request_readonly_observation_recovery(
        self,
        identifier: str,
        request_id: str,
        recovery_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Save one reviewed unavailable observation on the existing request."""
        from firefly_forge.readonly_observation_models import ReadonlyObservationRecoveryInputV1

        body = self._recovery_body(ReadonlyObservationRecoveryInputV1, request)
        path = (
            self._invalid_read_recovery_path(identifier, request_id)
            + "/readonly-observation-recoveries/"
            + self._recovery_identifier(recovery_id)
        )
        return dict(self.request("POST", path, json=body))

    def readonly_observation_recovery(self, identifier: str, request_id: str) -> dict[str, Any]:
        """Read the saved action after a lost response without another submission."""
        return dict(
            self.request(
                "GET", self._invalid_read_recovery_path(identifier, request_id) + "/readonly-observation-recovery"
            )
        )

    def _checkpoint_continuation_path(self, identifier: str, request_id: str) -> str:
        return (
            f"runs/{self._recovery_identifier(identifier)}/checkpoint-continuations/requests/"
            + self._recovery_identifier(request_id)
        )

    def checkpoint_continuation_candidate(self, identifier: str, request_id: str) -> dict[str, Any]:
        """Inspect the retained checkpoint and original ticket without admitting work."""
        return dict(self.request("GET", self._checkpoint_continuation_path(identifier, request_id) + "/candidate"))

    def request_checkpoint_continuation(
        self,
        identifier: str,
        request_id: str,
        action_id: str,
        request: CheckpointContinuationInputV1 | dict[str, Any],
    ) -> dict[str, Any]:
        """Save one finite same-ticket continuation; retain its action ID for readback."""
        from firefly_forge.checkpoint_continuation_models import CheckpointContinuationInputV1

        path = (
            self._checkpoint_continuation_path(identifier, request_id)
            + "/actions/"
            + self._recovery_identifier(action_id)
        )
        body = self._recovery_body(CheckpointContinuationInputV1, request)
        return dict(self.request("POST", path, json=body))

    def checkpoint_continuation_status(self, identifier: str, request_id: str) -> dict[str, Any]:
        """Read the saved action and worker status without submitting another action."""
        return dict(self.request("GET", self._checkpoint_continuation_path(identifier, request_id) + "/status"))

    def invalid_read_continuity_policy(self) -> dict[str, Any]:
        return dict(self.request("GET", "invalid-read-continuity/policy"))

    def configure_invalid_read_continuity_policy(
        self, request: ContinuityLeasePolicyV1 | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1

        body = self._recovery_body(ContinuityLeasePolicyV1, request)
        return dict(self.request("PUT", "invalid-read-continuity/policy", json=body))

    def get_run(self, identifier: str) -> dict[str, Any]:
        return dict(self.request("GET", f"runs/{identifier}"))

    def action(self, identifier: str, action: str) -> dict[str, Any]:
        if action not in {"cancel", "retry", "publish", "resume"}:
            raise ValueError("Unknown run action")
        return dict(self.request("POST", f"runs/{identifier}/{action}", **({"json": {}} if action == "resume" else {})))

    def plan(self, identifier: str) -> dict[str, Any]:
        return dict(self.request("GET", f"runs/{identifier}/plan"))

    def journal(self, identifier: str) -> dict[str, Any]:
        return dict(self.request("GET", f"runs/{identifier}/journal"))

    def execution_requests(self, identifier: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self.request("GET", f"runs/{self._id(identifier)}/execution-requests")]

    def request_runner_continuation(
        self, identifier: str, request: RunnerContinuationInput | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.runner_continuations import RunnerContinuationInput

        try:
            body = RunnerContinuationInput.model_validate(request)
        except ValueError:
            raise ValueError("Use exact 32-character request and execution identifiers; no additional fields") from None
        return dict(self.request("POST", f"runs/{self._id(identifier)}/runner-continuations", json=body.model_dump()))

    def runner_continuation(self, identifier: str, request_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"runs/{self._id(identifier)}/runner-continuations/{self._id(request_id)}"))

    def runner_continuation_candidates(self, identifier: str) -> dict[str, Any]:
        return dict(self.request("GET", f"runs/{self._id(identifier)}/runner-continuations/candidates"))

    def reconcile_execution_request(self, identifier: str, request_id: str, reconciliation_id: str) -> dict[str, Any]:
        from firefly_forge.execution_requests import ExecutionRequestReconciliation

        body = ExecutionRequestReconciliation(reconciliation_id=reconciliation_id)
        return dict(
            self.request(
                "POST",
                f"runs/{self._id(identifier)}/execution-requests/{self._id(request_id)}/reconcile",
                json=body.model_dump(),
            )
        )

    def handoff(
        self, identifier: str, agent: str | None = None, *, model_profile_id: str | None = None
    ) -> dict[str, Any]:
        from firefly_forge.controllers import ResumeRequest

        request = ResumeRequest.model_validate({"agent": agent, "model_profile_id": model_profile_id})
        if request.agent is None and request.model_profile_id is None:
            raise ValueError("Choose a coding provider or named model profile")
        return dict(self.request("POST", f"runs/{identifier}/handoff", json=request.model_dump(exclude_none=True)))

    def source_connections(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "scm/connections"))

    def connect_granted_repository(self, provider: str, remote_id: str) -> dict[str, Any]:
        if provider not in {"github", "gitlab", "bitbucket"}:
            raise ValueError("Unknown application provider")
        return dict(
            self.request("POST", f"scm/connections/{provider}/repositories/connect", json={"repository_id": remote_id})
        )

    def search_knowledge(self, repository_id: str, query: str) -> dict[str, Any]:
        return dict(self.request("POST", "knowledge/search", json={"repository_id": repository_id, "query": query}))

    @staticmethod
    def _framework_name(framework: str) -> str:
        if type(framework) is not str or framework not in {"pyfly", "larafly"}:
            raise ValueError("Select pyfly or larafly framework knowledge")
        return framework

    @staticmethod
    def _framework_page(cursor: str | None, page_size: int) -> dict[str, str | int]:
        if type(page_size) is not int or not 1 <= page_size <= 200:
            raise ValueError("Choose a framework response page size between 1 and 200")
        if cursor is not None and (type(cursor) is not str or not cursor):
            raise ValueError("Use the original framework page cursor")
        return {"page_size": page_size, **({"cursor": cursor} if cursor is not None else {})}

    def framework_knowledge_status(self) -> dict[str, Any]:
        return dict(self.request("GET", "knowledge/frameworks/status"))

    def check_framework_updates(
        self, framework: str, request: FrameworkCheckUpdatesInput | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.framework_knowledge_models import FrameworkCheckUpdatesInput

        framework = self._framework_name(framework)
        body = self._recovery_body(FrameworkCheckUpdatesInput, request)
        return dict(self.request("POST", f"knowledge/frameworks/{framework}/check-updates", json=body))

    def refresh_framework_knowledge(
        self, framework: str, request_id: str, request: FrameworkRefreshInput | dict[str, Any]
    ) -> dict[str, Any]:
        """Submit once; after an uncertain response, read the same retained operation."""
        from firefly_forge.framework_knowledge_models import FrameworkRefreshInput

        framework, request_id = self._framework_name(framework), self._recovery_identifier(request_id)
        body = self._recovery_body(FrameworkRefreshInput, request)
        return dict(self.request("POST", f"knowledge/frameworks/{framework}/refreshes/{request_id}", json=body))

    def framework_knowledge_refresh(self, framework: str, request_id: str) -> dict[str, Any]:
        framework, request_id = self._framework_name(framework), self._recovery_identifier(request_id)
        return dict(self.request("GET", f"knowledge/frameworks/{framework}/refreshes/{request_id}"))

    def observe_framework_knowledge_refresh(self, framework: str, request_id: str) -> dict[str, Any]:
        """Observe this operation once without resubmitting an uncertain source write."""
        framework, request_id = self._framework_name(framework), self._recovery_identifier(request_id)
        return dict(self.request("POST", f"knowledge/frameworks/{framework}/refreshes/{request_id}/observe"))

    def renew_framework_knowledge_budget(
        self, framework: str, request_id: str, request: FrameworkBudgetRenewalInput | dict[str, Any]
    ) -> dict[str, Any]:
        """Extend reviewed totals on the same operation, preserving all spent work."""
        from firefly_forge.framework_knowledge_models import FrameworkBudgetRenewalInput

        framework, request_id = self._framework_name(framework), self._recovery_identifier(request_id)
        body = self._recovery_body(FrameworkBudgetRenewalInput, request)
        return dict(self.request("POST", f"knowledge/frameworks/{framework}/refreshes/{request_id}/budget", json=body))

    def cancel_framework_knowledge_refresh(
        self, framework: str, request_id: str, request: FrameworkRefreshCancelInput | dict[str, Any]
    ) -> dict[str, Any]:
        """Cancel the reviewed operation without erasing uncertain submissions or receipts."""
        from firefly_forge.framework_knowledge_models import FrameworkRefreshCancelInput

        framework, request_id = self._framework_name(framework), self._recovery_identifier(request_id)
        body = self._recovery_body(FrameworkRefreshCancelInput, request)
        return dict(self.request("POST", f"knowledge/frameworks/{framework}/refreshes/{request_id}/cancel", json=body))

    def resume_framework_knowledge_refresh(
        self, framework: str, request_id: str, request: FrameworkRefreshResumeInput | dict[str, Any]
    ) -> dict[str, Any]:
        """Resume the same reviewed import; uncertain submissions need observation first."""
        from firefly_forge.framework_knowledge_models import FrameworkRefreshResumeInput

        framework, request_id = self._framework_name(framework), self._recovery_identifier(request_id)
        body = self._recovery_body(FrameworkRefreshResumeInput, request)
        return dict(self.request("POST", f"knowledge/frameworks/{framework}/refreshes/{request_id}/resume", json=body))

    def framework_corpora(self, framework: str, *, cursor: str | None = None, page_size: int = 50) -> dict[str, Any]:
        framework = self._framework_name(framework)
        params = self._framework_page(cursor, page_size)
        return dict(self.request("GET", f"knowledge/frameworks/{framework}/corpora", params=params))

    def framework_generation_candidates(
        self, framework: str, corpus_ref: str, *, cursor: str | None = None, page_size: int = 50
    ) -> dict[str, Any]:
        """List canonical validation evidence for this exact corpus before activation."""
        framework, corpus_ref = self._framework_name(framework), self._recovery_identifier(corpus_ref)
        cursor = self._recovery_identifier(cursor) if cursor is not None else None
        params = {"corpus_ref": corpus_ref, **self._framework_page(cursor, page_size)}
        return dict(self.request("GET", f"knowledge/frameworks/{framework}/generation-candidates", params=params))

    def activate_framework_generation_target(
        self, framework: str, request_id: str, request: FrameworkGenerationTargetInput | dict[str, Any]
    ) -> dict[str, Any]:
        """Activate a validated release once; read the same request after uncertainty."""
        from firefly_forge.framework_knowledge_models import FrameworkGenerationTargetInput

        framework, request_id = self._framework_name(framework), self._recovery_identifier(request_id)
        body = self._recovery_body(FrameworkGenerationTargetInput, request)
        return dict(
            self.request("POST", f"knowledge/frameworks/{framework}/generation-targets/{request_id}", json=body)
        )

    def get_framework_generation_target(self, framework: str, request_id: str) -> dict[str, Any]:
        framework, request_id = self._framework_name(framework), self._recovery_identifier(request_id)
        return dict(self.request("GET", f"knowledge/frameworks/{framework}/generation-targets/{request_id}"))

    def framework_corpus_sources(
        self, framework: str, corpus_ref: str, *, cursor: str | None = None, page_size: int = 50
    ) -> dict[str, Any]:
        framework, corpus_ref = self._framework_name(framework), self._recovery_identifier(corpus_ref)
        params = self._framework_page(cursor, page_size)
        return dict(
            self.request("GET", f"knowledge/frameworks/{framework}/corpora/{corpus_ref}/sources", params=params)
        )

    def search_framework_knowledge(
        self, framework: str, request: FrameworkSearchInput | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.framework_knowledge_models import FrameworkSearchInput

        framework = self._framework_name(framework)
        body = self._recovery_body(FrameworkSearchInput, request)
        return dict(self.request("POST", f"knowledge/frameworks/{framework}/search", json=body))

    def index_source(self, identifier: str, paths: list[str]) -> dict[str, Any]:
        return dict(self.request("POST", f"runs/{identifier}/knowledge/index", json={"paths": paths}))

    def remember(self, identifier: str) -> dict[str, Any]:
        return dict(self.request("POST", f"runs/{identifier}/knowledge/remember"))

    def deployment_targets(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "deployment-targets"))

    def deploy(self, identifier: str, target_id: str) -> dict[str, Any]:
        return dict(self.request("POST", f"runs/{identifier}/deploy", json={"target_id": target_id}))

    def rollback(self, identifier: str) -> dict[str, Any]:
        return dict(self.request("POST", f"deployments/{identifier}/rollback"))

    def product_definition(self, repository_id: str) -> dict[str, Any] | None:
        result = self.request("GET", f"repositories/{repository_id}/product")
        return dict(result) if result else None

    def save_product_definition(self, repository_id: str, definition: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.collaboration import ProductDefinition

        model = ProductDefinition.model_validate(definition)
        return dict(self.request("PUT", f"repositories/{repository_id}/product", json=model.model_dump()))

    def _product_change(
        self, repository_id: str, request: FeatureChangeInput | dict[str, Any], *, preview: bool
    ) -> dict[str, Any]:
        from firefly_forge.product_lifecycle import FeatureChangeInput

        model = FeatureChangeInput.model_validate(request)
        path = f"repositories/{self._id(repository_id)}/product/changes" + ("/preview" if preview else "")
        return dict(self.request("POST", path, json=model.model_dump(mode="json")))

    def preview_product_change(
        self, repository_id: str, request: FeatureChangeInput | dict[str, Any]
    ) -> dict[str, Any]:
        return self._product_change(repository_id, request, preview=True)

    def apply_product_change(self, repository_id: str, request: FeatureChangeInput | dict[str, Any]) -> dict[str, Any]:
        return self._product_change(repository_id, request, preview=False)

    @staticmethod
    def _model_profile_public(record: dict[str, Any], secrets: dict[str, str] | None = None) -> dict[str, Any]:
        from firefly_forge.configuration import FIELDS, SECRETS, redact_secrets

        fields = {"id", "name", "description", "provider", "revision", "tasks", "image_input", "generation_verified"}
        result = {key: value for key, value in record.items() if key in fields}
        public_config = {
            field + "_configured" if field in SECRETS else field for field in FIELDS.get(record.get("provider", ""), {})
        } | {"endpoint_invalid"}
        result["config"] = {key: value for key, value in record.get("config", {}).items() if key in public_config}

        def redact(value: Any) -> Any:
            if isinstance(value, str):
                return redact_secrets(value, secrets or {})
            if isinstance(value, dict):
                return {key: redact(item) for key, item in value.items()}
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value

        return dict(redact(result))

    def model_profiles(self) -> list[dict[str, Any]]:
        return [self._model_profile_public(dict(record)) for record in self.request("GET", "model-profiles")]

    def save_model_profile(self, name: str, request: ModelProfileInput | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.configuration import SECRETS
        from firefly_forge.model_profiles import ModelProfileInput, profile_id

        identifier = profile_id(name)
        try:
            model = ModelProfileInput.model_validate(request)
        except ValueError:
            raise ValueError("Invalid model profile configuration") from None
        try:
            result = self.request("PUT", f"model-profiles/{identifier}", json=model.model_dump())
        except RuntimeError:
            raise RuntimeError("Model profile configuration request failed; inspect server readiness") from None
        secrets = {"PROFILE_" + key.upper(): value for key, value in model.values.items() if key in SECRETS}
        return self._model_profile_public(dict(result), secrets)

    def test_model_profile(self, name: str) -> dict[str, Any]:
        from firefly_forge.model_profiles import profile_id

        return dict(self.request("POST", f"model-profiles/{profile_id(name)}/test", timeout=90))

    def task_routing(self) -> dict[str, Any]:
        return dict(self.request("GET", "task-routing"))

    def save_task_routing(self, request: TaskRoutingInput | MCPTaskRoutingInput | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.task_routing import TaskRoutingInput
        from firefly_forge.task_routing_mcp import TaskRoutingInput as MCPTaskRoutingInput

        body = request.model_dump() if isinstance(request, MCPTaskRoutingInput) else request
        model = TaskRoutingInput.model_validate(body)
        omitted = {"expected_digest"} if "expected_digest" not in model.model_fields_set else set()
        return dict(self.request("PUT", "task-routing", json=model.model_dump(exclude=omitted)))

    def repository_workflow(self, repository_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"repositories/{repository_id}/collaboration"))

    def repository_receipts(self, repository_id: str | None = None) -> list[dict[str, Any]]:
        return list(
            self.request(
                "GET", "collaboration/receipts", params={"repository_id": repository_id} if repository_id else {}
            )
        )

    def cloud_providers(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "cloud/providers"))

    def render_cloud_recipe(self, recipe: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.cloud import CloudRecipeInput

        model = CloudRecipeInput.model_validate(recipe)
        return dict(self.request("POST", "cloud/recipes/render", json=model.model_dump()))

    def applications(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "applications"))

    def application(self, identifier: str) -> dict[str, Any]:
        return dict(self.request("GET", f"applications/{identifier}"))

    def check_application(self, identifier: str) -> dict[str, Any]:
        return dict(self.request("POST", f"applications/{identifier}/check"))

    def incidents(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "incidents"))

    def maintain_incident(
        self, identifier: str, brief: str, checks: list[list[str]], agent: str = "codex"
    ) -> dict[str, Any]:
        from firefly_forge.application_operations import MaintenanceRequest

        model = MaintenanceRequest.model_validate(
            {"brief": brief, "agent": agent, "commands": [{"argv": argv} for argv in checks]}
        )
        return dict(self.request("POST", f"incidents/{identifier}/maintenance", json=model.model_dump()))

    def resolve_incident(self, identifier: str, resolution: str) -> dict[str, Any]:
        from firefly_forge.application_operations import IncidentResolution

        model = IncidentResolution(resolution=resolution)
        return dict(self.request("POST", f"incidents/{identifier}/resolve", json=model.model_dump()))

    @staticmethod
    def _id(value: str) -> str:
        try:
            return UUID(value).hex
        except ValueError as exc:
            raise ValueError("Use a valid factory identifier") from exc

    def projects(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "projects"))

    @staticmethod
    def _scm_actor_identifier(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
            raise ValueError("Use an exact factory identity for SCM delegation")
        return value

    def _scm_actor_path(self, project_id: str, repository_id: str) -> str:
        project = self._scm_actor_identifier(project_id)
        repository = self._scm_actor_identifier(repository_id)
        return f"projects/{project}/repositories/{repository}/scm-actor-bindings"

    def scm_actor_binding_candidates(
        self, project_id: str, repository_id: str, cursor: str | None = None
    ) -> dict[str, Any]:
        """Read at most fifty retained verified account references without provider discovery."""
        path = self._scm_actor_path(project_id, repository_id)
        params = {} if cursor is None else {"cursor": self._scm_actor_identifier(cursor)}
        return dict(self.request("GET", path + "/candidates", params=params))

    def configure_scm_actor_binding(
        self, project_id: str, repository_id: str, request: SCMActorBindingInputV1 | dict[str, Any]
    ) -> dict[str, Any]:
        """Explicitly delegate or revoke scoped actions using a stable request ID and prior digest."""
        from firefly_forge.scm_authority_models import SCMActorBindingInputV1

        path = self._scm_actor_path(project_id, repository_id)
        try:
            value = request.model_dump(warnings=False) if isinstance(request, SCMActorBindingInputV1) else request
            body = SCMActorBindingInputV1.model_validate(value)
        except ValueError:
            raise ValueError("Use a bounded SCM delegation with exact scope and request identities") from None
        return dict(self.request("POST", path, json=body.model_dump(mode="json")))

    def scm_actor_bindings(self, project_id: str, repository_id: str, cursor: str | None = None) -> dict[str, Any]:
        """Read bounded immutable delegation summaries; no grants or remote lookup are performed."""
        path = self._scm_actor_path(project_id, repository_id)
        params = {} if cursor is None else {"cursor": self._scm_actor_identifier(cursor)}
        return dict(self.request("GET", path, params=params))

    def scm_actor_binding(self, project_id: str, repository_id: str, request_id: str) -> dict[str, Any]:
        """Read the exact original administrative receipt after an uncertain response."""
        path = self._scm_actor_path(project_id, repository_id)
        return dict(self.request("GET", path + "/requests/" + self._scm_actor_identifier(request_id)))

    def _scm_poll_path(self, project_id: str, repository_id: str) -> str:
        project = self._scm_actor_identifier(project_id)
        repository = self._scm_actor_identifier(repository_id)
        return f"projects/{project}/repositories/{repository}/scm-polling"

    def scm_polling_setup(self, project_id: str, repository_id: str) -> dict[str, Any]:
        return dict(self.request("GET", self._scm_poll_path(project_id, repository_id) + "/setup"))

    def configure_scm_polling(self, project_id: str, repository_id: str, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.scm_poll_models import PollConfigurationV1

        body = PollConfigurationV1.model_validate(request)
        return dict(
            self.request(
                "POST",
                self._scm_poll_path(project_id, repository_id) + "/configuration",
                json=body.model_dump(mode="json"),
            )
        )

    def scm_polling_configuration(self, project_id: str, repository_id: str, request_id: str) -> dict[str, Any]:
        return dict(
            self.request(
                "GET",
                self._scm_poll_path(project_id, repository_id)
                + "/configuration/requests/"
                + self._scm_actor_identifier(request_id),
            )
        )

    def scm_polling_status(self, project_id: str, repository_id: str) -> dict[str, Any]:
        return dict(self.request("GET", self._scm_poll_path(project_id, repository_id) + "/status"))

    def scm_polling_candidates(self, project_id: str, repository_id: str) -> dict[str, Any]:
        return dict(self.request("GET", self._scm_poll_path(project_id, repository_id) + "/candidates"))

    def configure_scm_polling_binding(
        self, project_id: str, repository_id: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.scm_poll_models import SCMPollActorBindingInputV2

        body = SCMPollActorBindingInputV2.model_validate(request)
        return dict(
            self.request(
                "POST", self._scm_poll_path(project_id, repository_id) + "/bindings", json=body.model_dump(mode="json")
            )
        )

    def scm_polling_binding(self, project_id: str, repository_id: str, request_id: str) -> dict[str, Any]:
        return dict(
            self.request(
                "GET",
                self._scm_poll_path(project_id, repository_id)
                + "/bindings/requests/"
                + self._scm_actor_identifier(request_id),
            )
        )

    def scm_polling_heads(self, project_id: str, repository_id: str, after: str | None = None) -> dict[str, Any]:
        if after is not None and not re.fullmatch(r"[a-f0-9]{64}", after):
            raise ValueError("Use an exact polling binding key cursor")
        return dict(
            self.request(
                "GET",
                self._scm_poll_path(project_id, repository_id) + "/bindings",
                params={} if after is None else {"after": after},
            )
        )

    def _scm_source_edit_path(self, project_id: str, repository_id: str) -> str:
        project = self._scm_actor_identifier(project_id)
        repository = self._scm_actor_identifier(repository_id)
        return f"projects/{project}/repositories/{repository}/scm-polling/source-edit-grants"

    def configure_scm_source_edit_grant(
        self, project_id: str, repository_id: str, request: SCMSourceEditGrantInputV1 | dict[str, Any]
    ) -> dict[str, Any]:
        """Seal one exact native source activation; uncertainty permits original-result reads only."""
        from firefly_forge.scm_source_edit_models import SCMSourceEditGrantInputV1

        path = self._scm_source_edit_path(project_id, repository_id)
        try:
            value = request.model_dump(warnings=False) if isinstance(request, SCMSourceEditGrantInputV1) else request
            body = SCMSourceEditGrantInputV1.model_validate(value)
        except ValueError:
            raise ValueError("Use an exact bounded source-only consent and original request identity") from None
        return dict(self.request("POST", path, json=body.model_dump(mode="json")))

    def scm_source_edit_grant(self, project_id: str, repository_id: str, request_id: str) -> dict[str, Any]:
        path = self._scm_source_edit_path(project_id, repository_id)
        return dict(self.request("GET", path + "/requests/" + self._scm_actor_identifier(request_id)))

    def current_scm_source_edit_grant(
        self, project_id: str, repository_id: str, binding_request_id: str
    ) -> dict[str, Any]:
        path = self._scm_source_edit_path(project_id, repository_id)
        return dict(self.request("GET", path + "/current/" + self._scm_actor_identifier(binding_request_id)))

    def _scm_resume_grant_path(self, project_id: str, repository_id: str) -> str:
        project = self._scm_actor_identifier(project_id)
        repository = self._scm_actor_identifier(repository_id)
        return f"projects/{project}/repositories/{repository}/scm-resume-grants"

    def configure_scm_resume_grant(
        self, project_id: str, repository_id: str, request: SCMResumeGrantInputV2 | dict[str, Any]
    ) -> dict[str, Any]:
        """Submit one explicit V2 consent; preserve its request identity after an uncertain result."""
        from firefly_forge.scm_resume_models import SCMResumeGrantInputV2

        path = self._scm_resume_grant_path(project_id, repository_id)
        try:
            value = request.model_dump(warnings=False) if isinstance(request, SCMResumeGrantInputV2) else request
            body = SCMResumeGrantInputV2.model_validate(value)
        except ValueError:
            raise ValueError(
                "Use a bounded V2 resume consent with exact reviewed scope and request identities"
            ) from None
        return dict(self.request("POST", path, json=body.model_dump(mode="json")))

    def scm_resume_grant(self, project_id: str, repository_id: str, request_id: str) -> dict[str, Any]:
        """Read the exact retained consent; missing evidence never authorizes another write."""
        path = self._scm_resume_grant_path(project_id, repository_id)
        return dict(self.request("GET", path + "/requests/" + self._scm_actor_identifier(request_id)))

    def current_scm_resume_grant(self, project_id: str, repository_id: str, binding_request_id: str) -> dict[str, Any]:
        """Read the current consent for an exact V1 binding without dispatching a run action."""
        path = self._scm_resume_grant_path(project_id, repository_id)
        return dict(self.request("GET", path + "/current/" + self._scm_actor_identifier(binding_request_id)))

    def _scm_poll_resume_grant_path(self, project_id: str, repository_id: str) -> str:
        project = self._scm_actor_identifier(project_id)
        repository = self._scm_actor_identifier(repository_id)
        return f"projects/{project}/repositories/{repository}/scm-polling/resume-grants"

    def configure_scm_polling_resume_grant(
        self, project_id: str, repository_id: str, request: SCMPollResumeGrantInputV3 | dict[str, Any]
    ) -> dict[str, Any]:
        """Submit one explicit V3 polling consent; preserve its request identity after an uncertain result."""
        from firefly_forge.scm_poll_resume_models import SCMPollResumeGrantInputV3

        path = self._scm_poll_resume_grant_path(project_id, repository_id)
        try:
            value = request.model_dump(warnings=False) if isinstance(request, SCMPollResumeGrantInputV3) else request
            body = SCMPollResumeGrantInputV3.model_validate(value)
        except ValueError:
            raise ValueError(
                "Use a bounded V3 polling resume consent with exact reviewed scope and request identities"
            ) from None
        return dict(self.request("POST", path, json=body.model_dump(mode="json")))

    def scm_polling_resume_grant(self, project_id: str, repository_id: str, request_id: str) -> dict[str, Any]:
        """Read the exact retained consent; missing evidence never authorizes another write."""
        path = self._scm_poll_resume_grant_path(project_id, repository_id)
        return dict(self.request("GET", path + "/requests/" + self._scm_actor_identifier(request_id)))

    def current_scm_polling_resume_grant(
        self, project_id: str, repository_id: str, binding_request_id: str
    ) -> dict[str, Any]:
        """Read the current consent for an exact API-observed polling binding without dispatching a run action."""
        path = self._scm_poll_resume_grant_path(project_id, repository_id)
        return dict(self.request("GET", path + "/current/" + self._scm_actor_identifier(binding_request_id)))

    def review_scm_polling_resume(
        self, project_id: str, repository_id: str, binding_request_id: str, run_id: str
    ) -> dict[str, Any]:
        """Read the current same-run clarification cut; do not dispatch a command."""
        path = self._scm_poll_resume_grant_path(project_id, repository_id)
        return dict(
            self.request(
                "GET",
                path + "/review",
                params={
                    "binding_request_id": self._scm_actor_identifier(binding_request_id),
                    "run_id": self._scm_actor_identifier(run_id),
                },
            )
        )

    def _scm_poll_functional_grant_path(self, project_id: str, repository_id: str) -> str:
        project = self._scm_actor_identifier(project_id)
        repository = self._scm_actor_identifier(repository_id)
        return f"projects/{project}/repositories/{repository}/scm-polling/functional-grants"

    def configure_scm_polling_functional_grant(
        self, project_id: str, repository_id: str, request: SCMPollFunctionalGrantInputV1 | dict[str, Any]
    ) -> dict[str, Any]:
        """Submit one finite assistant consent; retain its identity after an uncertain result."""
        from firefly_forge.scm_poll_functional_models import SCMPollFunctionalGrantInputV1

        path = self._scm_poll_functional_grant_path(project_id, repository_id)
        try:
            value = (
                request.model_dump(warnings=False) if isinstance(request, SCMPollFunctionalGrantInputV1) else request
            )
            body = SCMPollFunctionalGrantInputV1.model_validate(value)
        except ValueError:
            raise ValueError(
                "Use a bounded V1 functional assistant consent with exact reviewed scope and request identities"
            ) from None
        return dict(self.request("POST", path, json=body.model_dump(mode="json")))

    def scm_polling_functional_grant(self, project_id: str, repository_id: str, request_id: str) -> dict[str, Any]:
        """Read the exact retained consent; missing evidence never authorizes another write."""
        path = self._scm_poll_functional_grant_path(project_id, repository_id)
        return dict(self.request("GET", path + "/requests/" + self._scm_actor_identifier(request_id)))

    def current_scm_polling_functional_grant(
        self, project_id: str, repository_id: str, binding_request_id: str
    ) -> dict[str, Any]:
        """Read the current consent for an exact API-observed polling binding without dispatching a run action."""
        path = self._scm_poll_functional_grant_path(project_id, repository_id)
        return dict(self.request("GET", path + "/current/" + self._scm_actor_identifier(binding_request_id)))

    def review_scm_polling_functional(
        self, project_id: str, repository_id: str, binding_request_id: str
    ) -> dict[str, Any]:
        """Read the current same-run clarification cut; do not dispatch a command."""
        path = self._scm_poll_functional_grant_path(project_id, repository_id)
        return dict(
            self.request(
                "GET",
                path + "/review",
                params={
                    "binding_request_id": self._scm_actor_identifier(binding_request_id),
                },
            )
        )

    def _scm_product_change_grant_path(self, project_id: str, repository_id: str) -> str:
        project = self._scm_actor_identifier(project_id)
        repository = self._scm_actor_identifier(repository_id)
        return f"projects/{project}/repositories/{repository}/scm-product-change-grants"

    def configure_scm_product_change_grant(
        self, project_id: str, repository_id: str, request: SCMProductChangeGrantInputV1 | dict[str, Any]
    ) -> dict[str, Any]:
        """Submit one explicit whole-product consent; preserve its request identity after an uncertain result."""
        from firefly_forge.scm_product_change_models import SCMProductChangeGrantInputV1

        path = self._scm_product_change_grant_path(project_id, repository_id)
        try:
            value = request.model_dump(warnings=False) if isinstance(request, SCMProductChangeGrantInputV1) else request
            body = SCMProductChangeGrantInputV1.model_validate(value)
        except ValueError:
            raise ValueError(
                "Use a bounded whole-product edit consent with exact reviewed scope and request identities"
            ) from None
        return dict(self.request("POST", path, json=body.model_dump(mode="json")))

    def scm_product_change_grant(self, project_id: str, repository_id: str, request_id: str) -> dict[str, Any]:
        """Read the exact retained consent; missing evidence never authorizes another write."""
        path = self._scm_product_change_grant_path(project_id, repository_id)
        return dict(self.request("GET", path + "/requests/" + self._scm_actor_identifier(request_id)))

    def current_scm_product_change_grant(
        self, project_id: str, repository_id: str, binding_request_id: str
    ) -> dict[str, Any]:
        """Read the current consent for an exact V1 binding without dispatching a run action."""
        path = self._scm_product_change_grant_path(project_id, repository_id)
        return dict(self.request("GET", path + "/current/" + self._scm_actor_identifier(binding_request_id)))

    def scm_product_change(self, project_id: str, repository_id: str, request_id: str) -> dict[str, Any]:
        """Read an immutable signed product-edit result; this never dispatches an edit."""
        project = self._scm_actor_identifier(project_id)
        repository = self._scm_actor_identifier(repository_id)
        request = self._scm_actor_identifier(request_id)
        return dict(
            self.request("GET", f"projects/{project}/repositories/{repository}/scm-product-changes/requests/{request}")
        )

    def configure_project(
        self, definition: ProjectDefinition | dict[str, Any], project_id: str | None = None
    ) -> dict[str, Any]:
        from firefly_forge.project_models import ProjectDefinition

        model = ProjectDefinition.model_validate(definition)
        path = "projects" + ("/" + self._id(project_id) if project_id else "")
        return dict(self.request("PUT" if project_id else "POST", path, json=model.model_dump(mode="json")))

    def configure_scm_product_intake_consent(
        self, repository_id: str, request: SCMProductIntakeConsentInputV1 | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.scm_product_intake_models import SCMProductIntakeConsentInputV1

        model = SCMProductIntakeConsentInputV1.model_validate(request)
        return dict(
            self.request(
                "POST",
                f"repositories/{self._id(repository_id)}/scm-product-intake/consents",
                json=model.model_dump(mode="json"),
            )
        )

    def scm_product_intake_consent(self, repository_id: str, request_id: str) -> dict[str, Any]:
        return dict(
            self.request(
                "GET", f"repositories/{self._id(repository_id)}/scm-product-intake/consents/{self._id(request_id)}"
            )
        )

    def scm_product_intake(self, repository_id: str, request_id: str) -> dict[str, Any]:
        return dict(
            self.request(
                "GET", f"repositories/{self._id(repository_id)}/scm-product-intake/requests/{self._id(request_id)}"
            )
        )

    def project(self, project_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"projects/{self._id(project_id)}"))

    def git_delivery_policy(self, project_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"projects/{self._id(project_id)}/git-delivery-policy"))

    def configure_git_deliveries(
        self, project_id: str, request: GitDeliveryPolicyInputV1 | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.git_delivery_models import GitDeliveryPolicyInputV1

        body = GitDeliveryPolicyInputV1.model_validate(request).model_dump(mode="json")
        return dict(self.request("PUT", f"projects/{self._id(project_id)}/git-delivery-policy", json=body))

    def git_deliveries(self, run_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"runs/{self._id(run_id)}/git-deliveries"))

    def request_git_delivery(self, run_id: str, request: GitDeliveryInputV1 | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.git_delivery_models import GitDeliveryInputV1

        body = GitDeliveryInputV1.model_validate(request).model_dump(mode="json")
        return dict(self.request("POST", f"runs/{self._id(run_id)}/git-deliveries", json=body))

    def git_delivery_request(self, run_id: str, request_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"runs/{self._id(run_id)}/git-deliveries/requests/{self._id(request_id)}"))

    def partial_source_candidate(self, run_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"runs/{self._id(run_id)}/git-deliveries/partial-source-candidate"))

    def request_partial_source_checkpoint(
        self, run_id: str, request: GitDeliveryInputV1 | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.git_delivery_models import GitDeliveryInputV1

        body = GitDeliveryInputV1.model_validate(request)
        if body.explicit_partial_source is None:
            raise ValueError("Partial source requires explicit source-only consent")
        return dict(
            self.request(
                "POST",
                f"runs/{self._id(run_id)}/git-deliveries/partial-source-requests",
                json=body.model_dump(mode="json"),
            )
        )

    def _diff_path(self, run_id: str, expected_manifest: str, request_id: str | None) -> str:
        if not isinstance(expected_manifest, str) or re.fullmatch(r"[a-f0-9]{64}", expected_manifest) is None:
            raise ValueError("Use an exact diff manifest SHA-256")
        path = f"runs/{self._id(run_id)}"
        if request_id is not None:
            path += f"/git-deliveries/requests/{self._id(request_id)}"
        return path + "/diff"

    def run_diff_index(
        self, run_id: str, expected_manifest: str, *, page: int = 0, request_id: str | None = None
    ) -> GitDiffIndexPageV1:
        from firefly_forge.git_diff_models import GitDiffIndexPageV1

        if type(page) is not int or page < 0:
            raise ValueError("Use a nonnegative diff index page")
        path = self._diff_path(run_id, expected_manifest, request_id)
        if request_id is None:
            path += "/index"
        result = GitDiffIndexPageV1.model_validate(
            self.request("GET", path, params={"expected_manifest": expected_manifest, "page": page})
        )
        if (
            result.manifest_sha256 != expected_manifest
            or result.page != page
            or re.fullmatch(r"[a-f0-9]{32}", result.artifact_id) is None
            or result.next_page not in (None, page + 1)
        ):
            raise ValueError("The returned diff index differs from the requested page")
        return result

    def run_diff_file(
        self,
        run_id: str,
        expected_manifest: str,
        *,
        file_index: int,
        offset: int = 0,
        limit: int = 32768,
        request_id: str | None = None,
    ) -> GitDiffPageV1:
        from firefly_forge.git_diff_models import GitDiffPageV1

        if (
            type(file_index) is not int
            or file_index < 0
            or type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 1048576
        ):
            raise ValueError("Use a bounded nonnegative diff file window")
        path = self._diff_path(run_id, expected_manifest, request_id) + f"/files/{file_index}"
        result = GitDiffPageV1.model_validate(
            self.request("GET", path, params={"expected_manifest": expected_manifest, "offset": offset, "limit": limit})
        )
        if len(result.content_base64) != 4 * ((result.byte_count + 2) // 3):
            raise ValueError("The returned diff content encoding exceeds its declared window")
        content = base64.b64decode(result.content_base64, validate=True)
        if (
            result.manifest_sha256 != expected_manifest
            or re.fullmatch(r"[a-f0-9]{32}", result.artifact_id) is None
            or result.file_index != file_index
            or result.offset != offset
            or result.byte_count > limit
            or (result.byte_count == 0 and offset < result.total_bytes)
            or offset + result.byte_count > result.total_bytes
            or result.next_offset
            != (offset + result.byte_count if offset + result.byte_count < result.total_bytes else None)
            or len(content) != result.byte_count
            or hashlib.sha256(content).hexdigest() != result.content_sha256
        ):
            raise ValueError("The returned diff content differs from the requested verified window")
        return result

    def analytics_configuration(self, project_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"projects/{self._id(project_id)}/analytics/configuration"))

    def configure_analytics(
        self, project_id: str, request: AnalyticsConfigurationInput | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.analytics_models import AnalyticsConfigurationInput

        model = AnalyticsConfigurationInput.model_validate(request)
        body = model.model_dump(mode="json", exclude={"agent_token"})
        body["agent_token"] = model.agent_token.get_secret_value()
        return dict(self.request("PUT", f"projects/{self._id(project_id)}/analytics/configuration", json=body))

    def analytics_catalog(self, project_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"projects/{self._id(project_id)}/analytics/catalog"))

    def analytics_table(self, project_id: str, table_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"projects/{self._id(project_id)}/analytics/tables/{UUID(table_id)}"))

    def analytics_schema_object(self, project_id: str, table_id: str, object_id: str) -> dict[str, Any]:
        return dict(
            self.request(
                "GET", f"projects/{self._id(project_id)}/analytics/tables/{UUID(table_id)}/objects/{UUID(object_id)}"
            )
        )

    def git_delivery_observations(self, run_id: str, request_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"runs/{self._id(run_id)}/git-deliveries/observations/{self._id(request_id)}"))

    def project_revision(self, project_id: str, revision: int) -> dict[str, Any]:
        if isinstance(revision, bool) or revision < 1:
            raise ValueError("Project revisions are positive integers")
        return dict(self.request("GET", f"projects/{self._id(project_id)}/revisions/{revision}"))

    def project_analyses(self, project_id: str) -> list[dict[str, Any]]:
        return list(self.request("GET", f"projects/{self._id(project_id)}/analyses"))

    def analyze_project(self, project_id: str, component_ids: list[str] | None = None) -> dict[str, Any]:
        from firefly_forge.reverse_engineering import AnalysisInput

        model = AnalysisInput(component_ids=component_ids or [])
        return dict(
            self.request("POST", f"projects/{self._id(project_id)}/analyses", json=model.model_dump(), timeout=360)
        )

    def project_analysis(self, project_id: str, analysis_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"projects/{self._id(project_id)}/analyses/{self._id(analysis_id)}"))

    def review_project_analysis(
        self, project_id: str, analysis_id: str, review: AnalysisReview | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.reverse_engineering import AnalysisReview

        model = AnalysisReview.model_validate(review)
        return dict(
            self.request(
                "POST",
                f"projects/{self._id(project_id)}/analyses/{self._id(analysis_id)}/review",
                json=model.model_dump(),
            )
        )

    def assistant_conversations(self, repository_id: str) -> list[dict[str, Any]]:
        return list(self.request("GET", f"repositories/{self._id(repository_id)}/assistant/conversations"))

    def create_assistant_conversation(
        self,
        repository_id: str,
        *,
        agent: str | None = None,
        project_id: str | None = None,
        task: Literal["product", "functions", "design", "architecture"] = "product",
        model_profile_id: str | None = None,
        analysis_id: str | None = None,
        analysis_digest: str | None = None,
        analysis_component_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        from firefly_forge.product_assistant import AssistantSessionInput

        values = {
            "task": task,
            **{
                key: value
                for key, value in {
                    "agent": agent,
                    "project_id": project_id,
                    "model_profile_id": model_profile_id,
                    "analysis_id": analysis_id,
                    "analysis_digest": analysis_digest,
                    "analysis_component_ids": analysis_component_ids,
                }.items()
                if value is not None
            },
        }
        model = AssistantSessionInput.model_validate(values)
        return dict(
            self.request(
                "POST",
                f"repositories/{self._id(repository_id)}/assistant/conversations",
                json=model.model_dump(exclude_unset=True),
            )
        )

    def assistant_conversation(self, repository_id: str, conversation_id: str) -> dict[str, Any]:
        return dict(
            self.request(
                "GET", f"repositories/{self._id(repository_id)}/assistant/conversations/" + self._id(conversation_id)
            )
        )

    def assistant_turn(
        self, repository_id: str, conversation_id: str, request: AssistantTurnInput | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.product_assistant import AssistantTurnInput

        model = AssistantTurnInput.model_validate(request)
        return dict(
            self.request(
                "POST",
                f"repositories/{self._id(repository_id)}/assistant/conversations/"
                + self._id(conversation_id)
                + "/turns",
                json=model.model_dump(),
                timeout=360,
            )
        )

    def assistant_capabilities(self, repository_id: str, conversation_id: str) -> dict[str, Any]:
        return dict(
            self.request(
                "GET",
                f"repositories/{self._id(repository_id)}/assistant/conversations/"
                + self._id(conversation_id)
                + "/capabilities",
            )
        )

    def recover_assistant_turn(
        self,
        repository_id: str,
        conversation_id: str,
        turn_id: str,
        request: AssistantRecoveryInput | dict[str, Any],
    ) -> dict[str, Any]:
        from firefly_forge.product_assistant import AssistantRecoveryInput

        model = AssistantRecoveryInput.model_validate(request)
        return dict(
            self.request(
                "POST",
                f"repositories/{self._id(repository_id)}/assistant/conversations/"
                + self._id(conversation_id)
                + f"/turns/{self._id(turn_id)}/recover",
                json=model.model_dump(),
            )
        )

    @staticmethod
    def _attachment_metadata(value: dict[str, Any]) -> dict[str, Any]:
        fields = {
            "id",
            "request_id",
            "filename",
            "mime_type",
            "kind",
            "content_bytes",
            "sha256",
            "status",
            "text_chars",
            "truncated",
            "created_at",
            "detail",
        }
        return {key: item for key, item in value.items() if key in fields}

    def _attachment_path(self, repository_id: str, conversation_id: str) -> str:
        return f"repositories/{self._id(repository_id)}/assistant/conversations/{self._id(conversation_id)}/attachments"

    def assistant_attachments(self, repository_id: str, conversation_id: str) -> list[dict[str, Any]]:
        return [
            self._attachment_metadata(dict(item))
            for item in self.request("GET", self._attachment_path(repository_id, conversation_id))
        ]

    def assistant_attachment(self, repository_id: str, conversation_id: str, attachment_id: str) -> dict[str, Any]:
        return self._attachment_metadata(
            dict(
                self.request(
                    "GET", self._attachment_path(repository_id, conversation_id) + "/" + self._id(attachment_id)
                )
            )
        )

    def upload_assistant_attachment(
        self, repository_id: str, conversation_id: str, request: AttachmentInput | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.assistant_attachments import AttachmentInput

        try:
            model = AttachmentInput.model_validate(request)
            content = base64.b64decode(model.content_base64, validate=True)
            if not content or len(content) > 1_000_000:
                raise ValueError("Invalid attachment size")
            result = self.request(
                "POST", self._attachment_path(repository_id, conversation_id), json=model.model_dump()
            )
        except (ValueError, RuntimeError, httpx.HTTPError):
            raise ValueError(
                "Attachment upload failed. Check its name, format and 1 MB limit; inspect the conversation "
                "before retrying the same request ID."
            ) from None
        return self._attachment_metadata(dict(result))

    def agent_roles(self) -> dict[str, Any]:
        return dict(self.request("GET", "agent-roles"))

    def agent_role_lifecycle(self) -> dict[str, Any]:
        return dict(self.request("GET", "agent-roles/lifecycle"))

    def repository_role_lifecycle(self, repository_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"repositories/{self._id(repository_id)}/agent-roles/lifecycle"))

    def identity_configuration(self) -> dict[str, Any]:
        return dict(self.request("GET", "auth/configuration"))

    def identity_session(self) -> dict[str, Any]:
        return dict(self.request("GET", "auth/session"))

    def project_members(self, project_id: str) -> list[dict[str, Any]]:
        return list(self.request("GET", f"projects/{self._id(project_id)}/members"))

    def set_project_membership(
        self, project_id: str, user_id: str, request: MembershipInput | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.access_controllers import MembershipInput

        try:
            body = MembershipInput.model_validate(request, strict=True).model_dump(mode="json")
        except ValueError:
            raise ValueError("Invalid project membership request") from None
        return dict(self.request("PUT", f"projects/{self._id(project_id)}/members/{self._id(user_id)}", json=body))

    def identity_users(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "identity/users"))

    def project_eligible_users(self, project_id: str, query: str) -> list[dict[str, Any]]:
        query = query.strip()
        if not 3 <= len(query) <= 120:
            raise ValueError("Enter between 3 and 120 characters to find a registered user")
        return list(self.request("GET", f"projects/{self._id(project_id)}/eligible-users", params={"query": query}))

    def identity_administration(self) -> dict[str, Any]:
        return dict(self.request("GET", "identity/administration"))

    def identity_operations(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "identity/operations"))

    @staticmethod
    def _identity_body(model: type[BaseModel], request: BaseModel | dict[str, Any]) -> dict[str, Any]:
        try:
            return model.model_validate(request, strict=True).model_dump(mode="json")
        except ValueError:
            # Validation must not reflect forbidden passwords or role shortcuts.
            raise ValueError("Invalid identity administration request") from None

    def set_identity_user_enabled(self, user_id: str, enabled: bool) -> dict[str, Any]:
        from firefly_forge.user_administration import UserEnabledInput

        body = self._identity_body(UserEnabledInput, {"enabled": enabled})
        return dict(self.request("PUT", f"identity/users/{self._id(user_id)}", json=body))

    def provision_identity_user(self, request: UserProvisionInput | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.user_administration import UserProvisionInput

        body = self._identity_body(UserProvisionInput, request)
        return dict(self.request("POST", "identity/users", json=body))

    def invite_identity_user(self, user_id: str, request: UserInviteInput | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.user_administration import UserInviteInput

        body = self._identity_body(UserInviteInput, request)
        return dict(self.request("POST", f"identity/users/{self._id(user_id)}/invite", json=body))

    def reconcile_identity_operation(
        self, operation_id: str, request: AdministrationReconciliation | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.user_administration import AdministrationReconciliation

        body = self._identity_body(AdministrationReconciliation, request)
        return dict(self.request("POST", f"identity/operations/{self._id(operation_id)}/reconcile", json=body))

    def recover_identity_operation(
        self, operation_id: str, request: AdministrationRecovery | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.user_administration import AdministrationRecovery

        if isinstance(request, dict) and request.get("owner_stopped") is not True:
            raise ValueError("Explicit owner-stopped confirmation is required")
        body = self._identity_body(AdministrationRecovery, request)
        return dict(self.request("POST", f"identity/operations/{self._id(operation_id)}/recover", json=body))

    def save_agent_roles(self, document: RoleDocument | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.agent_roles import RoleDocument

        model = RoleDocument.model_validate(document)
        return dict(self.request("PUT", "agent-roles", json=model.model_dump(mode="json")))

    def repository_roles(self, repository_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"repositories/{self._id(repository_id)}/agent-roles"))

    def save_repository_roles(self, repository_id: str, roles: ProjectRoles | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.agent_roles import ProjectRoles

        model = ProjectRoles.model_validate(roles)
        return dict(self.request("PUT", f"repositories/{self._id(repository_id)}/agent-roles", json=model.model_dump()))

    def worker_messages(self, run_id: str) -> list[dict[str, Any]]:
        return list(self.request("GET", f"runs/{self._id(run_id)}/worker-messages"))

    def worker_evidence(self, run_id: str, request_id: str) -> list[WorkerImage]:
        from firefly_forge.worker_evidence import WorkerImage

        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", request_id):
            raise ValueError("Use an exact recorded worker request identifier")
        value = self.request("GET", f"runs/{self._id(run_id)}/worker-evidence", params={"request_id": request_id})
        if not isinstance(value, list) or len(value) > 6:
            raise ValueError("Worker evidence must be a bounded list of retained images")
        images = [WorkerImage.model_validate(item) for item in value]
        if len({image.id for image in images}) != len(images):
            raise ValueError("Worker evidence contains duplicate image identities")
        return images

    def worker_image(self, run_id: str, image: WorkerImage | dict[str, Any]) -> bytes:
        import hashlib

        from firefly_forge.worker_evidence import WorkerImage

        selected = WorkerImage.model_validate(image)
        with self.http.stream("GET", f"runs/{self._id(run_id)}/worker-evidence/{selected.id}") as response:
            if not response.is_success:
                raise RuntimeError(f"Factory image request failed (HTTP {response.status_code})")
            if (
                response.headers.get("content-type", "").split(";", 1)[0] != selected.mime_type
                or response.headers.get("x-content-sha256") != selected.sha256
            ):
                raise ValueError("Worker image metadata does not match its retained identity")
            data = bytearray()
            for chunk in response.iter_bytes():
                if len(data) + len(chunk) > selected.bytes:
                    raise ValueError("Worker image exceeds its retained byte count")
                data.extend(chunk)
        magic = {
            "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": data.startswith(b"\xff\xd8\xff"),
            "image/webp": data[:4] == b"RIFF" and data[8:12] == b"WEBP",
        }
        if (
            len(data) != selected.bytes
            or hashlib.sha256(data).hexdigest() != selected.sha256
            or not magic[selected.mime_type]
        ):
            raise ValueError("Worker image bytes do not match their retained hash and raster type")
        return bytes(data)

    def checkpoint_mode_candidate(self, run_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"runs/{self._id(run_id)}/checkpoint-mode-adoptions/candidate"))

    def request_checkpoint_mode_adoption(
        self, run_id: str, request_id: str, request: CheckpointModeAdoptionInput | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.checkpoint_mode_models import CheckpointModeAdoptionInput

        body = CheckpointModeAdoptionInput.model_validate(request)
        return dict(
            self.request(
                "POST",
                f"runs/{self._id(run_id)}/checkpoint-mode-adoptions/{self._id(request_id)}",
                json=body.model_dump(),
            )
        )

    def get_checkpoint_mode_adoption(self, run_id: str, request_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"runs/{self._id(run_id)}/checkpoint-mode-adoptions/{self._id(request_id)}"))

    def original_selection_candidate(self, run_id: str) -> dict[str, Any]:
        run_id = self._recovery_identifier(run_id)
        return dict(self.request("GET", f"runs/{run_id}/original-selection-readmissions/candidate"))

    def request_original_selection_readmission(
        self, run_id: str, request_id: str, request: OriginalSelectionReadmissionInput | dict[str, Any]
    ) -> dict[str, Any]:
        """Submit once; after an uncertain response, read this same admission identity."""
        from firefly_forge.original_selection_models import OriginalSelectionReadmissionInput

        run_id, request_id = self._recovery_identifier(run_id), self._recovery_identifier(request_id)
        body = self._recovery_body(OriginalSelectionReadmissionInput, request)
        return dict(self.request("POST", f"runs/{run_id}/original-selection-readmissions/{request_id}", json=body))

    def get_original_selection_readmission(self, run_id: str, request_id: str) -> dict[str, Any]:
        run_id, request_id = self._recovery_identifier(run_id), self._recovery_identifier(request_id)
        return dict(self.request("GET", f"runs/{run_id}/original-selection-readmissions/{request_id}"))

    def verification_handoff_candidates(self, run_id: str, agent: str) -> dict[str, Any]:
        if agent not in {"codex", "claude", "bridge", "agentic"}:
            raise ValueError("Choose an explicit configured coding provider")
        return dict(
            self.request("GET", f"runs/{self._id(run_id)}/verification-handoffs/candidates", params={"agent": agent})
        )

    def unknown_command_acknowledgement_candidate(
        self, run_id: str, execution_id: str, *, agent: str, adoption_request_id: str
    ) -> dict[str, Any]:
        if agent not in {"codex", "claude", "bridge", "agentic"}:
            raise ValueError("Choose an explicit configured coding provider")
        return dict(
            self.request(
                "GET",
                f"runs/{self._id(run_id)}/sandbox-executions/{self._id(execution_id)}/unknown-command-acknowledgements/candidate",
                params={"agent": agent, "adoption_request_id": self._id(adoption_request_id)},
            )
        )

    def acknowledge_unknown_command(
        self,
        run_id: str,
        execution_id: str,
        request_id: str,
        request: UnknownCommandAcknowledgementV1 | dict[str, Any],
    ) -> dict[str, Any]:
        from firefly_forge.unknown_command_models import UnknownCommandAcknowledgementV1

        body = UnknownCommandAcknowledgementV1.model_validate(request)
        return dict(
            self.request(
                "POST",
                f"runs/{self._id(run_id)}/sandbox-executions/{self._id(execution_id)}/unknown-command-acknowledgements/{self._id(request_id)}",
                json=body.model_dump(),
            )
        )

    def unknown_command_acknowledgement(self, run_id: str, execution_id: str, request_id: str) -> dict[str, Any]:
        return dict(
            self.request(
                "GET",
                f"runs/{self._id(run_id)}/sandbox-executions/{self._id(execution_id)}/unknown-command-acknowledgements/{self._id(request_id)}",
            )
        )

    def adopt_verification_handoff(self, run_id: str, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.verification_models import VerificationAdoption

        body = VerificationAdoption.model_validate(request)
        return dict(
            self.request("POST", f"runs/{self._id(run_id)}/verification-handoffs/adoptions", json=body.model_dump())
        )

    def verification_handoff_adoption(self, run_id: str, request_id: str) -> dict[str, Any]:
        return dict(
            self.request("GET", f"runs/{self._id(run_id)}/verification-handoffs/adoptions/{self._id(request_id)}")
        )

    def respond_worker(self, run_id: str, request_id: str, response: WorkerResponse | dict[str, Any]) -> dict[str, Any]:
        import re

        from firefly_forge.agent_roles import WorkerResponse

        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", request_id):
            raise ValueError("Use the worker request's exact identifier")
        model = WorkerResponse.model_validate(response)
        return dict(
            self.request(
                "POST", f"runs/{self._id(run_id)}/worker-requests/{request_id}/respond", json=model.model_dump()
            )
        )

    def run_evidence(self, identifier: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        from firefly_forge.run_evidence import EvidenceQuery

        selected = EvidenceQuery.model_validate(query or {})
        return dict(
            self.request("GET", f"runs/{self._id(identifier)}/evidence", params=selected.model_dump(exclude_none=True))
        )

    def export_run_evidence(self, identifier: str, expected_cut: str) -> bytes:
        from firefly_forge.run_evidence import EvidenceExportInput

        selected = EvidenceExportInput(expected_cut=expected_cut)
        response = self.http.get(
            f"runs/{self._id(identifier)}/evidence.zip", params={"expected_cut": selected.expected_cut}
        )
        response.raise_for_status()
        return response.content

    def run_activity_content(self, run_id: str, event_id: str, query: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.activity_models import ActivityContentQuery

        selected = ActivityContentQuery.model_validate(query)
        return dict(
            self.request(
                "GET",
                f"runs/{self._id(run_id)}/activity/{self._id(event_id)}/content",
                params=selected.model_dump(),
            )
        )

    def export(self, identifier: str) -> bytes:
        response = self.http.get(f"runs/{identifier}/artifacts/changes.zip")
        response.raise_for_status()
        return response.content

    def project_campaigns(self, project_id: str) -> list[dict[str, Any]]:
        return list(self.request("GET", f"projects/{self._id(project_id)}/campaigns"))

    def campaign_execution_policy(self, project_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"projects/{self._id(project_id)}/campaign-execution-policy"))

    def save_campaign_execution_policy(
        self, project_id: str, request: CampaignExecutionPolicyInputV1 | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.campaign_execution_budget import CampaignExecutionPolicyInputV1

        try:
            body = CampaignExecutionPolicyInputV1.model_validate(request)
        except ValueError:
            raise ValueError(
                "Use an exact project policy request, previous digest and finite execution window"
            ) from None
        return dict(
            self.request("PUT", f"projects/{self._id(project_id)}/campaign-execution-policy", json=body.model_dump())
        )

    def renew_campaign_budget(
        self, project_id: str, campaign_id: str, request: CampaignBudgetRenewalV1 | dict[str, Any]
    ) -> dict[str, Any]:
        from firefly_forge.campaign_execution_budget import CampaignBudgetRenewalV1

        try:
            body = CampaignBudgetRenewalV1.model_validate(request)
        except ValueError:
            raise ValueError("Use an exact campaign request, journal head, policy digest and finite renewal") from None
        return dict(
            self.request(
                "POST",
                f"projects/{self._id(project_id)}/campaigns/{self._id(campaign_id)}/renew-budget",
                json=body.model_dump(),
            )
        )

    def launch_campaign(self, project_id: str, request: CampaignLaunch | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.project_campaigns import CampaignLaunch

        model = CampaignLaunch.model_validate(request)
        return dict(
            self.request("POST", f"projects/{self._id(project_id)}/campaigns", json=model.model_dump(), timeout=360)
        )

    def project_campaign(self, project_id: str, campaign_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"projects/{self._id(project_id)}/campaigns/{self._id(campaign_id)}"))

    def project_campaign_report(self, project_id: str, campaign_id: str) -> dict[str, Any]:
        """Read one local campaign evidence snapshot; this does not contact source control."""
        return dict(self.request("GET", f"projects/{self._id(project_id)}/campaigns/{self._id(campaign_id)}/report"))

    def archive_campaign_report(
        self, project_id: str, campaign_id: str, request: CampaignReportArchiveInput | dict[str, Any]
    ) -> dict[str, Any]:
        """Explicitly retain the selected report digest under the caller's stable request identity."""
        from firefly_forge.campaign_report_history import CampaignReportArchiveInput

        try:
            body = CampaignReportArchiveInput.model_validate(request)
        except ValueError:
            raise ValueError(
                "Use an exact request identifier and selected report digest; no additional fields"
            ) from None
        return dict(
            self.request(
                "POST",
                f"projects/{self._id(project_id)}/campaigns/{self._id(campaign_id)}/report-archives",
                json=body.model_dump(),
            )
        )

    def campaign_report_archive(self, project_id: str, campaign_id: str, request_id: str) -> dict[str, Any]:
        return dict(
            self.request(
                "GET",
                f"projects/{self._id(project_id)}/campaigns/{self._id(campaign_id)}/report-archives/{self._id(request_id)}",
            )
        )

    def campaign_report_archives(self, project_id: str, campaign_id: str, after: int = 0) -> dict[str, Any]:
        if type(after) is not int or not 0 <= after <= 100:
            raise ValueError("Archive history cursor must be an integer between 0 and 100")
        return dict(
            self.request(
                "GET",
                f"projects/{self._id(project_id)}/campaigns/{self._id(campaign_id)}/report-archives",
                params={"after": after},
            )
        )

    def save_campaign_delivery_plan(
        self, project_id: str, campaign_id: str, request: CampaignDeliveryPlanInput | dict[str, Any]
    ) -> dict[str, Any]:
        """Save explicit post-admission reporting criteria, never execution instructions."""
        from firefly_forge.campaign_delivery_plan_models import CampaignDeliveryPlanInput

        try:
            body = CampaignDeliveryPlanInput.model_validate(request)
        except ValueError:
            raise ValueError("Use a bounded reporting plan with exact request and source identities") from None
        return dict(
            self.request(
                "POST",
                f"projects/{self._id(project_id)}/campaigns/{self._id(campaign_id)}/delivery-plan-revisions",
                json=body.model_dump(),
            )
        )

    def campaign_delivery_plans(self, project_id: str, campaign_id: str) -> dict[str, Any]:
        return dict(
            self.request(
                "GET",
                f"projects/{self._id(project_id)}/campaigns/{self._id(campaign_id)}/delivery-plan-revisions",
            )
        )

    def campaign_delivery_plan(self, project_id: str, campaign_id: str, request_id: str) -> dict[str, Any]:
        path = f"projects/{self._id(project_id)}/campaigns/{self._id(campaign_id)}/delivery-plan-revisions"
        return dict(self.request("GET", f"{path}/{self._id(request_id)}"))

    def campaign_delivery_progress(
        self, project_id: str, campaign_id: str, request_id: str, report_request_id: str
    ) -> dict[str, Any]:
        path = f"projects/{self._id(project_id)}/campaigns/{self._id(campaign_id)}/delivery-plan-revisions"
        return dict(
            self.request(
                "GET",
                f"{path}/{self._id(request_id)}/progress",
                params={"report_request_id": self._id(report_request_id)},
            )
        )

    def _campaign_action(self, project_id: str, campaign_id: str, operation: str, request: Any) -> dict[str, Any]:
        from firefly_forge.project_campaigns import CampaignAction, CampaignRetry

        model = (CampaignRetry if operation == "retry" else CampaignAction).model_validate(request)
        return dict(
            self.request(
                "POST",
                f"projects/{self._id(project_id)}/campaigns/{self._id(campaign_id)}/{operation}",
                json=model.model_dump(),
            )
        )

    def cancel_campaign(
        self, project_id: str, campaign_id: str, request: CampaignAction | dict[str, Any]
    ) -> dict[str, Any]:
        return self._campaign_action(project_id, campaign_id, "cancel", request)

    def resume_campaign(
        self, project_id: str, campaign_id: str, request: CampaignAction | dict[str, Any]
    ) -> dict[str, Any]:
        return self._campaign_action(project_id, campaign_id, "resume", request)

    def retry_campaign(
        self, project_id: str, campaign_id: str, request: CampaignRetry | dict[str, Any]
    ) -> dict[str, Any]:
        return self._campaign_action(project_id, campaign_id, "retry", request)

    def repository_runner(self, repository_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"repositories/{self._id(repository_id)}/runner"))

    def save_repository_runner(self, repository_id: str, sandbox: SandboxInput | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.sandbox_models import SandboxInput

        model = SandboxInput.model_validate(sandbox)
        return dict(
            self.request("PUT", f"repositories/{self._id(repository_id)}/runner", json={"sandbox": model.model_dump()})
        )

    def runner_readiness(self, repository_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"repositories/{self._id(repository_id)}/runner/readiness", timeout=90))

    def probe_runner(self, sandbox: SandboxInput | dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.sandbox_models import SandboxInput

        model = SandboxInput.model_validate(sandbox)
        return dict(self.request("POST", "runners/probe", json=model.model_dump(), timeout=90))

    @staticmethod
    def _search_metadata(record: dict[str, Any]) -> dict[str, Any]:
        fields = {
            "provider",
            "endpoint",
            "revision",
            "configured",
            "max_results",
            "live_search_verified",
            "include_domains",
        }
        return {key: value for key, value in record.items() if key in fields}

    def search_configuration(self) -> dict[str, Any]:
        return self._search_metadata(dict(self.request("GET", "search/configuration")))

    def external_mcp_catalog(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "tools/mcp"))

    @staticmethod
    def _oauth_name(name: str) -> str:
        if name == "providers" or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
            raise ValueError("Invalid OAuth connection name")
        return name

    def oauth_provider_policies(self) -> dict[str, Any]:
        return dict(self.request("GET", "oauth-provider-policies"))

    def configure_local_oauth_provider_policy(self, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.oauth_provider_policy_models import OAuthProviderPolicyInput

        payload = OAuthProviderPolicyInput.model_validate(request).model_dump()
        return dict(self.request("PUT", "oauth-provider-policies/local-repository-tools", json=payload))

    def connection_oauth_providers(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "connection-oauth/providers"))

    def connection_oauth_connections(self, project_id: str | None = None) -> list[dict[str, Any]]:
        if project_id is not None and not re.fullmatch(r"[a-f0-9]{32}", project_id):
            raise ValueError("Select an exact project identifier")
        return list(self.request("GET", "connection-oauth", params={"project_id": project_id} if project_id else {}))

    def connection_oauth(self, name: str) -> dict[str, Any]:
        return dict(self.request("GET", f"connection-oauth/{self._oauth_name(name)}"))

    def connection_oauth_effect(self, name: str, request_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("Select an exact effect request identifier")
        return dict(self.request("GET", f"connection-oauth/{self._oauth_name(name)}/effects/{request_id}"))

    def supersede_connection_oauth(self, name: str, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.connection_oauth_models import OAuthSupersedeInput

        model = OAuthSupersedeInput.model_validate(request)
        payload = model.model_dump(exclude={"registration": {"client_secret"}})
        if model.registration.client_secret is not None:
            payload["registration"]["client_secret"] = model.registration.client_secret.get_secret_value()
        return dict(self.request("POST", f"connection-oauth/{self._oauth_name(name)}/supersede", json=payload))

    def configure_connection_oauth(self, name: str, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.connection_oauth_models import OAuthConnectionInput

        model = OAuthConnectionInput.model_validate(request)
        payload = model.model_dump(exclude={"client_secret"})
        if model.client_secret is not None:
            payload["client_secret"] = model.client_secret.get_secret_value()
        return dict(self.request("PUT", f"connection-oauth/{self._oauth_name(name)}", json=payload))

    def prepare_connection_oauth(self, name: str) -> dict[str, Any]:
        return dict(self.request("POST", f"connection-oauth/{self._oauth_name(name)}/prepare"))

    def refresh_connection_oauth(self, name: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.connection_oauth_controllers import OAuthRefreshInput

        body = OAuthRefreshInput(request_id=request_id)
        return dict(self.request("POST", f"connection-oauth/{self._oauth_name(name)}/refresh", json=body.model_dump()))

    def disconnect_connection_oauth(self, name: str) -> dict[str, Any]:
        return dict(self.request("POST", f"connection-oauth/{self._oauth_name(name)}/disconnect"))

    def configure_external_mcp(self, connector: str, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.mcp_registry import MCPConnectionInput, _connector

        _connector(connector)
        model = MCPConnectionInput.model_validate(request)
        return dict(self.request("PUT", f"tools/mcp/{connector}", json=model.model_dump()))

    def link_external_mcp_oauth(self, connector: str, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.mcp_registry import MCPOAuthConnectionInput, _connector

        _connector(connector)
        model = MCPOAuthConnectionInput.model_validate(request)
        return dict(self.request("PUT", f"tools/mcp/{connector}/oauth", json=model.model_dump()))

    def discover_external_mcp(self, connector: str) -> dict[str, Any]:
        from firefly_forge.mcp_registry import _connector

        _connector(connector)
        return dict(self.request("POST", f"tools/mcp/{connector}/discover"))

    def review_external_mcp(self, connector: str, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.mcp_registry import MCPReviewInput, _connector

        _connector(connector)
        model = MCPReviewInput.model_validate(request)
        return dict(self.request("POST", f"tools/mcp/{connector}/review", json=model.model_dump()))

    def external_mcp_grants(self, project_id: str) -> list[dict[str, Any]]:
        return list(self.request("GET", f"tools/mcp/projects/{self._id(project_id)}/grants"))

    def grant_external_mcp(self, project_id: str, connector: str, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.mcp_registry import MCPGrantInput, _connector

        _connector(connector)
        model = MCPGrantInput.model_validate(request)
        return dict(
            self.request(
                "POST",
                f"tools/mcp/projects/{self._id(project_id)}/connectors/{connector}/grants",
                json=model.model_dump(),
            )
        )

    def revoke_external_mcp(self, project_id: str, grant_id: str) -> dict[str, Any]:
        return dict(
            self.request("POST", f"tools/mcp/projects/{self._id(project_id)}/grants/{self._id(grant_id)}/revoke")
        )

    def tool_policy(self) -> dict[str, Any]:
        return dict(self.request("GET", "tools/policy"))

    def learning_policy(self) -> dict[str, Any]:
        return dict(self.request("GET", "learning/policy"))

    def configure_learning_policy(self, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.learning_models import LearningPolicy

        model = LearningPolicy.model_validate(request)
        return dict(self.request("PUT", "learning/policy", json=model.model_dump()))

    def learning_records(self, repository_id: str) -> list[dict[str, Any]]:
        return list(self.request("GET", f"repositories/{self._id(repository_id)}/learning"))

    def invalidate_learning(self, record_id: str, reason: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.learning_controllers import LearningInvalidation

        body = LearningInvalidation(reason=reason, request_id=request_id)
        return dict(self.request("POST", f"learning/records/{self._id(record_id)}/invalidate", json=body.model_dump()))

    def learning_invalidation_receipt(self, record_id: str, request_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"learning/records/{self._id(record_id)}/invalidations/{self._id(request_id)}"))

    def learning_projection_observation_receipt(self, record_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.learning_models import LearningProjectionObservationReceipt

        result = self.request(
            "GET", f"learning/records/{self._id(record_id)}/projection/observations/{self._id(request_id)}"
        )
        return LearningProjectionObservationReceipt.model_validate(result).model_dump(mode="json")

    def learning_projection(self, record_id: str) -> dict[str, Any]:
        return dict(self.request("GET", f"learning/records/{self._id(record_id)}/projection"))

    def submit_learning_projection(self, record_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.learning_controllers import LearningAction

        body = LearningAction(request_id=request_id)
        return dict(
            self.request("POST", f"learning/records/{self._id(record_id)}/projection/submit", json=body.model_dump())
        )

    def observe_learning_projection(
        self, record_id: str, request_id: str, source_id: str | None = None
    ) -> dict[str, Any]:
        from firefly_forge.learning_controllers import LearningObservation

        body = LearningObservation(request_id=request_id, source_id=source_id)
        return dict(
            self.request("POST", f"learning/records/{self._id(record_id)}/projection/observe", json=body.model_dump())
        )

    def observe_learning_projection_attempt(self, record_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Send one v2 observation attempt. Recover its exact receipt instead of retrying."""
        from pydantic import ValidationError

        from firefly_forge.learning_observation_attempt_models import (
            LearningObservationAttemptInput,
            LearningObservationAttemptReceipt,
        )

        try:
            body = LearningObservationAttemptInput.model_validate(request)
        except ValidationError:
            raise ValueError("A bounded v2 observation request with exact projection anchors is required") from None
        result = self.request(
            "POST", f"learning/records/{self._id(record_id)}/projection/observation-attempts", json=body.model_dump()
        )
        return LearningObservationAttemptReceipt.model_validate(result).model_dump(mode="json")

    def learning_projection_observation_attempt(self, record_id: str, attempt_id: str) -> dict[str, Any]:
        """Read a local attempt; absence or an unresolved state never authorizes replay."""
        from firefly_forge.learning_observation_attempt_models import (
            LearningObservationAttemptReceipt,
            observation_attempt_id,
        )

        result = self.request(
            "GET",
            "learning/records/"
            + self._id(record_id)
            + "/projection/observation-attempts/"
            + observation_attempt_id(attempt_id),
        )
        return LearningObservationAttemptReceipt.model_validate(result).model_dump(mode="json")

    def configure_tool_policy(self, request: dict[str, Any]) -> dict[str, Any]:
        from firefly_forge.tool_broker import FactoryToolPolicyInput, ToolPolicyInput

        model = (FactoryToolPolicyInput if "automatic_verification" in request else ToolPolicyInput).model_validate(
            request
        )
        return dict(self.request("PUT", "tools/policy", json=model.model_dump()))

    def tool_invocations(self, run_id: str) -> list[dict[str, Any]]:
        return list(self.request("GET", f"runs/{self._id(run_id)}/tool-invocations"))

    def abandon_tool_invocation(self, run_id: str, correlation_id: str, request_id: str) -> dict[str, Any]:
        from firefly_forge.tool_controllers import ToolReconciliationInput

        body = ToolReconciliationInput(request_id=request_id)
        return dict(
            self.request(
                "POST",
                f"runs/{self._id(run_id)}/tool-invocations/{self._id(correlation_id)}/reconcile",
                json=body.model_dump(),
            )
        )

    def configure_search(
        self,
        *,
        api_key: str | None = None,
        clear_secret: bool = False,
        provider: str | None = None,
        include_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        from firefly_forge.runner_configuration import SearchConfiguration

        if api_key is None and not clear_secret and provider is None and include_domains is None:
            raise ValueError("Supply a private key or a search configuration change")
        model = SearchConfiguration.model_validate(
            {"api_key": api_key, "clear_secret": clear_secret, "provider": provider, "include_domains": include_domains}
        )
        result = self.request(
            "PUT",
            "search/configuration",
            json=model.model_dump(exclude_none=True),
        )
        return self._search_metadata(dict(result))

    def test_search(self) -> dict[str, Any]:
        return dict(self.request("POST", "search/test", timeout=30))

    def internet_search(self, query: str, max_results: int = 5) -> dict[str, Any]:
        from firefly_forge.runner_configuration import SearchQuery

        model = SearchQuery(query=query, max_results=max_results)
        return dict(self.request("POST", "search/query", json=model.model_dump(), timeout=30))

    def search_citation(self, citation_id: str) -> dict[str, Any]:
        import re

        if not re.fullmatch(r"web-[a-f0-9]{24}", citation_id):
            raise ValueError("Use a valid search citation identifier")
        return dict(self.request("GET", "search/citations/" + citation_id))

    def browser_screenshot(self, evidence_id: str) -> bytes:
        with self.http.stream("GET", f"runner-evidence/{self._id(evidence_id)}/screenshot") as response:
            if response.is_error:
                raise RuntimeError(f"Factory screenshot request failed (HTTP {response.status_code})")
            if response.headers.get("content-type", "").split(";", 1)[0].lower() != "image/png":
                raise ValueError("Browser evidence must be a PNG image")
            data = bytearray()
            for chunk in response.iter_bytes():
                if len(data) + len(chunk) > 2_000_000:
                    raise ValueError("Browser evidence exceeds 2 MB")
                data.extend(chunk)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Browser evidence contains an invalid PNG header")
        return bytes(data)

    def download_browser_screenshot(self, evidence_id: str, destination: Path) -> dict[str, Any]:
        import hashlib
        import os

        data = self.browser_screenshot(evidence_id)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        return {
            "evidence_id": self._id(evidence_id),
            "path": str(destination.absolute()),
            "media_type": "image/png",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
