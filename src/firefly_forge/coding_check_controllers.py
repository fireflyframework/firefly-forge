# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Separate administrator review and immutable configured-check grant receipts."""

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, QueryParam, get_mapping, post_mapping, request_mapping

from firefly_forge.coding_check_grants import CodingCheckGrants
from firefly_forge.coding_check_models import (
    CodingCheckCandidate,
    CodingCheckCurrentStatus,
    CodingCheckGrantInput,
    CodingCheckGrantReceipt,
    CodingCheckRevocationInput,
    CodingCheckRevocationReceipt,
)


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/coding-check-grants")
class CodingCheckGrantController:
    def __init__(self, grants: CodingCheckGrants) -> None:
        self.grants = grants

    @get_mapping("/candidate")
    async def candidate(
        self, run_id: PathVar[str], recovery_request_id: QueryParam[str], bind_port: QueryParam[int] = 0
    ) -> CodingCheckCandidate:
        return CodingCheckCandidate.model_validate(await self.grants.candidate(run_id, recovery_request_id, bind_port))

    @get_mapping("/current")
    async def current(self, run_id: PathVar[str]) -> CodingCheckCurrentStatus:
        return CodingCheckCurrentStatus.model_validate(await self.grants.status(run_id))

    @post_mapping("", status_code=201)
    async def grant(self, run_id: PathVar[str], body: Body[CodingCheckGrantInput]) -> CodingCheckGrantReceipt:
        return CodingCheckGrantReceipt.model_validate(await self.grants.grant(run_id, body))

    @post_mapping("/revocations", status_code=201)
    async def revoke(
        self, run_id: PathVar[str], body: Body[CodingCheckRevocationInput]
    ) -> CodingCheckRevocationReceipt:
        return CodingCheckRevocationReceipt.model_validate(await self.grants.revoke(run_id, body))

    @get_mapping("/{request_id}")
    async def receipt(
        self, run_id: PathVar[str], request_id: PathVar[str]
    ) -> CodingCheckGrantReceipt | CodingCheckRevocationReceipt:
        value = await self.grants.get(run_id, request_id)
        if "candidate" in value:
            return CodingCheckGrantReceipt.model_validate(value)
        return CodingCheckRevocationReceipt.model_validate(value)
