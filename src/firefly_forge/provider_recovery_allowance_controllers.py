# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit administrator review, reservation and exact final-allowance readback."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, QueryParam, get_mapping, post_mapping, request_mapping

from firefly_forge.provider_recovery_allowances import ProviderRecoveryAllowanceInput, ProviderRecoveryAllowances


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/provider-recoveries/allowances")
class ProviderRecoveryAllowanceController:
    def __init__(self, allowances: ProviderRecoveryAllowances) -> None:
        self.allowances = allowances

    @get_mapping("/candidate")
    async def candidate(
        self, run_id: PathVar[str], agent: QueryParam[str], adopt_current_executable: QueryParam[bool] = False
    ) -> dict[str, Any]:
        return await self.allowances.candidate(run_id, agent, adopt_current_executable=adopt_current_executable)

    @post_mapping("", status_code=201)
    async def reserve(self, run_id: PathVar[str], body: Body[ProviderRecoveryAllowanceInput]) -> dict[str, Any]:
        return await self.allowances.reserve(run_id, body)

    @get_mapping("/{request_id}")
    async def receipt(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.allowances.get(run_id, request_id)
