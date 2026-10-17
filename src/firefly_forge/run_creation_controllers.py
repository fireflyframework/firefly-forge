# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit original run admission, with no fallback to legacy creation."""

import json
from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, request_mapping
from starlette.responses import Response

from firefly_forge.run_creation_models import RunCreationCancelInput, RunCreationInput
from firefly_forge.run_creation_requests import RunCreationRequests


def _response(value: dict[str, Any], status: int = 200) -> Response:
    return Response(
        json.dumps(value, sort_keys=True, separators=(",", ":")), status_code=status, media_type="application/json"
    )


@rest_controller
@request_mapping("/api/v1/run-creation-requests")
class RunCreationController:
    def __init__(self, requests: RunCreationRequests) -> None:
        self.requests = requests

    @post_mapping("/{request_id}")
    async def create(self, request_id: PathVar[str], body: Body[RunCreationInput]) -> Response:
        receipt, created = await self.requests.admit(request_id, body)
        return _response(receipt, 201 if created else 200)

    @get_mapping("/{request_id}")
    async def get(self, request_id: PathVar[str]) -> Response:
        return _response(await self.requests.get(request_id))

    @post_mapping("/{request_id}/cancel")
    async def cancel(self, request_id: PathVar[str], body: Body[RunCreationCancelInput]) -> Response:
        decision, created = await self.requests.cancel(request_id, body)
        return _response(decision, 201 if created else 200)

    @get_mapping("/{request_id}/decision")
    async def decision(self, request_id: PathVar[str]) -> Response:
        return _response(await self.requests.decision(request_id))
