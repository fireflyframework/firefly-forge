# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit report storage and exact history reads, without execution effects."""

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, QueryParam, get_mapping, post_mapping, request_mapping

from firefly_forge.campaign_report_history import (
    CampaignReportArchiveInput,
    CampaignReportArchivePage,
    CampaignReportArchiveReceipt,
    CampaignReportHistory,
)


@rest_controller
@request_mapping("/api/v1/projects/{project_id}/campaigns/{campaign_id}/report-archives")
class CampaignReportHistoryController:
    def __init__(self, history: CampaignReportHistory) -> None:
        self.manager = history

    @post_mapping("")
    async def archive(
        self, project_id: PathVar[str], campaign_id: PathVar[str], body: Body[CampaignReportArchiveInput]
    ) -> CampaignReportArchiveReceipt:
        return CampaignReportArchiveReceipt.model_validate(await self.manager.archive(project_id, campaign_id, body))

    @get_mapping("")
    async def history(
        self, project_id: PathVar[str], campaign_id: PathVar[str], after: QueryParam[int] = 0
    ) -> CampaignReportArchivePage:
        return CampaignReportArchivePage.model_validate(await self.manager.list(project_id, campaign_id, after))

    @get_mapping("/{request_id}")
    async def receipt(
        self, project_id: PathVar[str], campaign_id: PathVar[str], request_id: PathVar[str]
    ) -> CampaignReportArchiveReceipt:
        return CampaignReportArchiveReceipt.model_validate(await self.manager.get(project_id, campaign_id, request_id))
