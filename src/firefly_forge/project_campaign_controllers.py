# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticated, project-scoped delivery campaign admission and explicit recovery."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, put_mapping, request_mapping

from firefly_forge.campaign_execution_budget import CampaignBudgetRenewalV1, CampaignExecutionPolicyInputV1
from firefly_forge.campaign_reports import CampaignReport, CampaignReports
from firefly_forge.project_campaigns import CampaignAction, CampaignLaunch, CampaignRetry, ProjectCampaigns


@rest_controller
@request_mapping("/api/v1/projects/{project_id}/campaign-execution-policy")
class CampaignExecutionPolicyController:
    def __init__(self, campaigns: ProjectCampaigns) -> None:
        self.manager = campaigns

    @get_mapping("")
    async def get(self, project_id: PathVar[str]) -> dict[str, Any]:
        return await self.manager.execution_policies.view(project_id)

    @put_mapping("")
    async def configure(self, project_id: PathVar[str], body: Body[CampaignExecutionPolicyInputV1]) -> dict[str, Any]:
        return await self.manager.execution_policies.configure(project_id, body)


@rest_controller
@request_mapping("/api/v1/projects/{project_id}/campaigns")
class ProjectCampaignController:
    def __init__(self, campaigns: ProjectCampaigns, reports: CampaignReports) -> None:
        self.manager, self.reports = campaigns, reports

    @get_mapping("")
    async def campaigns(self, project_id: PathVar[str]) -> list[dict[str, Any]]:
        return await self.manager.campaigns(project_id)

    @post_mapping("", status_code=201)
    async def launch(self, project_id: PathVar[str], body: Body[CampaignLaunch]) -> dict[str, Any]:
        return await self.manager.launch(project_id, body)

    @get_mapping("/{campaign_id}")
    async def campaign(self, project_id: PathVar[str], campaign_id: PathVar[str]) -> dict[str, Any]:
        return await self.manager.campaign(project_id, campaign_id)

    @get_mapping("/{campaign_id}/report")
    async def report(self, project_id: PathVar[str], campaign_id: PathVar[str]) -> CampaignReport:
        return CampaignReport.model_validate(await self.reports.report(project_id, campaign_id))

    @post_mapping("/{campaign_id}/cancel")
    async def cancel(
        self, project_id: PathVar[str], campaign_id: PathVar[str], body: Body[CampaignAction]
    ) -> dict[str, Any]:
        return await self.manager.cancel(project_id, campaign_id, body)

    @post_mapping("/{campaign_id}/resume")
    async def resume(
        self, project_id: PathVar[str], campaign_id: PathVar[str], body: Body[CampaignAction]
    ) -> dict[str, Any]:
        return await self.manager.resume(project_id, campaign_id, body)

    @post_mapping("/{campaign_id}/retry")
    async def retry(
        self, project_id: PathVar[str], campaign_id: PathVar[str], body: Body[CampaignRetry]
    ) -> dict[str, Any]:
        return await self.manager.retry(project_id, campaign_id, body)

    @post_mapping("/{campaign_id}/renew-budget")
    async def renew_budget(
        self, project_id: PathVar[str], campaign_id: PathVar[str], body: Body[CampaignBudgetRenewalV1]
    ) -> dict[str, Any]:
        return await self.manager.renew_budget(project_id, campaign_id, body)
