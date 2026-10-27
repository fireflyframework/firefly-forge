# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Namespaced native continuity with explicit same-attempt lease acknowledgement."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, put_mapping, request_mapping

from firefly_forge.invalid_read_continuities import InvalidReadContinuities
from firefly_forge.invalid_read_continuity_models import InvalidReadAdmissionInputV1, InvalidReadAdmissionInputV2
from firefly_forge.invalid_read_lease import ContinuityLeasePolicies, ContinuityLeasePolicyV1
from firefly_forge.invalid_read_recovery_models import InvalidReadRecoveryInputV1


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

    @post_mapping("/requests-v2/{request_id}", status_code=201)
    async def request_unavailable_observations(
        self, run_id: PathVar[str], request_id: PathVar[str], body: Body[InvalidReadAdmissionInputV2]
    ) -> dict[str, Any]:
        return await self.continuities.request(run_id, request_id, body)

    async def _recovery(self, action: Any, *arguments: Any) -> dict[str, Any]:
        try:
            return dict(await action(*arguments))
        except KeyError as exc:
            raise KeyError("Continuity recovery not found") from exc
        except ValueError as exc:
            raise ValueError(
                "Continuity recovery is unavailable; inspect the retained request and current authority"
            ) from exc

    @get_mapping("/requests/{request_id}/recovery-candidate")
    async def recovery_candidate(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self._recovery(self.continuities.recoveries.candidate, run_id, request_id)

    @post_mapping("/requests/{request_id}/recoveries/{recovery_id}", status_code=201)
    async def recover_unclaimed(
        self,
        run_id: PathVar[str],
        request_id: PathVar[str],
        recovery_id: PathVar[str],
        body: Body[InvalidReadRecoveryInputV1],
    ) -> dict[str, Any]:
        return await self._recovery(self.continuities.recoveries.request, run_id, request_id, recovery_id, body)

    @get_mapping("/requests/{request_id}/recovery")
    async def recovery(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self._recovery(self.continuities.recoveries.status, run_id, request_id)

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
