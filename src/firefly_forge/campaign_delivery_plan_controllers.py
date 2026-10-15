# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit post-admission reporting plans; reads never start campaign work."""

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, QueryParam, get_mapping, post_mapping, request_mapping

from firefly_forge.campaign_delivery_plan_models import (
    CampaignDeliveryPlanInput,
    CampaignDeliveryPlanPage,
    CampaignDeliveryPlanReceipt,
    CampaignDeliveryProgress,
)
from firefly_forge.campaign_delivery_plans import CampaignDeliveryPlans


@rest_controller
@request_mapping("/api/v1/projects/{project_id}/campaigns/{campaign_id}/delivery-plan-revisions")
class CampaignDeliveryPlanController:
    def __init__(self, plans: CampaignDeliveryPlans) -> None:
        self.manager = plans

    @post_mapping("")
    async def save(
        self, project_id: PathVar[str], campaign_id: PathVar[str], body: Body[CampaignDeliveryPlanInput]
    ) -> CampaignDeliveryPlanReceipt:
        return CampaignDeliveryPlanReceipt.model_validate(await self.manager.save(project_id, campaign_id, body))

    @get_mapping("")
    async def plans(self, project_id: PathVar[str], campaign_id: PathVar[str]) -> CampaignDeliveryPlanPage:
        return CampaignDeliveryPlanPage.model_validate(await self.manager.list(project_id, campaign_id))

    @get_mapping("/{request_id}")
    async def receipt(
        self, project_id: PathVar[str], campaign_id: PathVar[str], request_id: PathVar[str]
    ) -> CampaignDeliveryPlanReceipt:
        return CampaignDeliveryPlanReceipt.model_validate(await self.manager.get(project_id, campaign_id, request_id))

    @get_mapping("/{request_id}/progress")
    async def progress(
        self,
        project_id: PathVar[str],
        campaign_id: PathVar[str],
        request_id: PathVar[str],
        report_request_id: QueryParam[str],
    ) -> CampaignDeliveryProgress:
        return CampaignDeliveryProgress.model_validate(
            await self.manager.progress(project_id, campaign_id, request_id, report_request_id)
        )
