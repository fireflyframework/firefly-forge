# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Read a retained failure, explicitly admit one recovery, or read its exact receipt."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, QueryParam, get_mapping, post_mapping, request_mapping

from firefly_forge.provider_recoveries import ProviderRecoveries
from firefly_forge.provider_recovery_models import ProviderRecoveryAdmission


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/provider-recoveries")
class ProviderRecoveryController:
    def __init__(self, recoveries: ProviderRecoveries) -> None:
        self.recoveries = recoveries

    @get_mapping("/candidate")
    async def candidate(
        self, run_id: PathVar[str], agent: QueryParam[str], adopt_current_executable: QueryParam[bool] = False
    ) -> dict[str, Any]:
        return await self.recoveries.candidate(run_id, agent, adopt_current_executable=adopt_current_executable)

    @post_mapping("", status_code=201)
    async def admit(self, run_id: PathVar[str], body: Body[ProviderRecoveryAdmission]) -> dict[str, Any]:
        return await self.recoveries.admit(run_id, body)

    @get_mapping("/{request_id}")
    async def receipt(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.recoveries.get(run_id, request_id)
