# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native authenticated checkpoint policy and owner-queued delivery endpoints."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, put_mapping, request_mapping

from firefly_forge.git_deliveries import GitDeliveries
from firefly_forge.git_delivery_models import GitDeliveryInputV1, GitDeliveryPolicyInputV1
from firefly_forge.git_delivery_policy import GitDeliveryPolicies
from firefly_forge.git_delivery_requests import GitDeliveryRequests


@rest_controller
@request_mapping("/api/v1/projects/{project_id}/git-delivery-policy")
class GitDeliveryPolicyController:
    def __init__(self, policies: GitDeliveryPolicies) -> None:
        self.policies = policies

    @get_mapping("")
    async def get(self, project_id: PathVar[str]) -> dict[str, Any]:
        return await self.policies.view(project_id)

    @put_mapping("")
    async def configure(self, project_id: PathVar[str], body: Body[GitDeliveryPolicyInputV1]) -> dict[str, Any]:
        return await self.policies.configure(project_id, body)


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/git-deliveries")
class GitDeliveryController:
    def __init__(self, deliveries: GitDeliveries, tickets: GitDeliveryRequests) -> None:
        self.deliveries, self.tickets = deliveries, tickets

    @get_mapping("")
    async def list(self, run_id: PathVar[str]) -> dict[str, Any]:
        return await self.deliveries.view(run_id)

    @get_mapping("/adoption-candidate")
    async def adoption_candidate(self, run_id: PathVar[str]) -> dict[str, Any]:
        from firefly_forge.git_failed_unit_adoption import GitFailedUnitAdoptions
        from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess

        user = CURRENT_PRINCIPAL.get()
        if user is None:
            raise AccessDenied("Sign in before reviewing a failed-unit checkpoint")
        return await GitFailedUnitAdoptions(self.deliveries).candidate(run_id, ProjectAccess.actor_snapshot(user))

    @post_mapping("", status_code=202)
    async def request(self, run_id: PathVar[str], body: Body[GitDeliveryInputV1]) -> dict[str, Any]:
        return await self.tickets.request(run_id, body)

    @get_mapping("/candidate")
    async def candidate(self, run_id: PathVar[str]) -> dict[str, Any]:
        return await self.deliveries.candidate(run_id)

    @get_mapping("/requests/{request_id}")
    async def ticket(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.tickets.get(run_id, request_id)

    @get_mapping("/observations/{request_id}")
    async def observations(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.deliveries.observations(run_id, request_id)
