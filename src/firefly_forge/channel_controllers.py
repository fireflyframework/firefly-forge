# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Operator bootstrap setup and signed application-wide repository ingress."""

from __future__ import annotations

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, put_mapping, request_mapping
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from firefly_forge.channel_actions import BootstrapSettings, RepositoryChannels


@rest_controller
@request_mapping("/api/v1")
class ChannelController:
    def __init__(self, channels: RepositoryChannels) -> None:
        self.channels = channels

    @get_mapping("/collaboration/bootstrap/{provider}")
    def settings(self, provider: PathVar[str]) -> dict[str, Any]:
        return self.channels.settings(provider)

    @put_mapping("/collaboration/bootstrap/{provider}")
    async def configure(self, provider: PathVar[str], body: Body[BootstrapSettings]) -> dict[str, Any]:
        return await self.channels.configure(provider, body)

    @get_mapping("/collaboration/bootstrap/{provider}/receipts")
    async def receipts(self, provider: PathVar[str]) -> list[dict[str, Any]]:
        return await self.channels.receipts(provider)

    @post_mapping("/webhooks/{provider}", status_code=202)
    async def webhook(self, provider: PathVar[str], request: Request) -> Response:
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 100000:
                return JSONResponse({"detail": "Webhook body exceeds 100 KB"}, status_code=413)
            raw.extend(chunk)
        receipt = await self.channels.receive(provider, bytes(raw), request.headers)
        return JSONResponse(receipt, status_code=202)
