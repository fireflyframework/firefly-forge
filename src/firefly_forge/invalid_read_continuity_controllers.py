# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Namespaced native continuity with explicit same-attempt lease acknowledgement."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, put_mapping, request_mapping

from firefly_forge.invalid_read_continuities import InvalidReadContinuities
from firefly_forge.invalid_read_continuity_models import InvalidReadAdmissionInputV1
from firefly_forge.invalid_read_lease import ContinuityLeasePolicies, ContinuityLeasePolicyV1


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/invalid-read-continuity")
class InvalidReadContinuityController:
    def __init__(self, continuities: InvalidReadContinuities) -> None:
        self.continuities = continuities

    @get_mapping("/candidate")
    async def candidate(self, run_id: PathVar[str]) -> dict[str, Any]:
        return await self.continuities.candidate(run_id)

    @post_mapping("/requests/{request_id}", status_code=201)
    async def request(
        self, run_id: PathVar[str], request_id: PathVar[str], body: Body[InvalidReadAdmissionInputV1]
    ) -> dict[str, Any]:
        return await self.continuities.request(run_id, request_id, body)

    @get_mapping("/requests/{request_id}")
    async def get(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.continuities.get(run_id, request_id)


@rest_controller
@request_mapping("/api/v1/invalid-read-continuity/policy")
class ContinuityLeasePolicyController:
    def __init__(self, policies: ContinuityLeasePolicies) -> None:
        self.policies = policies

    @get_mapping("")
    async def get(self) -> dict[str, Any]:
        return await self.policies.get()

    @put_mapping("")
    async def configure(self, body: Body[ContinuityLeasePolicyV1]) -> dict[str, Any]:
        return await self.policies.configure(body)
