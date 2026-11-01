# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit consent and readback for an independently owned verification operation."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping

from firefly_forge.linked_verification_models import LinkedVerificationInputV1
from firefly_forge.linked_verifications import LinkedVerifications


@rest_controller
@request_mapping("/api/v1/runs/{run_id}/linked-verifications")
class LinkedVerificationController:
    def __init__(self, verifications: LinkedVerifications) -> None:
        self.verifications = verifications

    @get_mapping("/candidate")
    async def candidate(self, run_id: PathVar[str]) -> dict[str, Any]:
        return await self.verifications.candidate(run_id)

    @post_mapping("", status_code=201)
    async def request(self, run_id: PathVar[str], body: Body[LinkedVerificationInputV1]) -> dict[str, Any]:
        return await self.verifications.request(run_id, body)

    @get_mapping("/{request_id}")
    async def status(self, run_id: PathVar[str], request_id: PathVar[str]) -> dict[str, Any]:
        return await self.verifications.get(run_id, request_id)
