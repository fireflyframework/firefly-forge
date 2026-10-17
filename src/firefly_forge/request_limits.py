# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bound streamed API bodies before JSON parsing, including chunked requests."""

from __future__ import annotations

import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestLimits:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        limit = 100_000
        if re.fullmatch(r"/api/v1/run-creation-requests/[a-f0-9]{32}", scope["path"]):
            limit = 256 * 1024
        if re.fullmatch(r"/api/v1/run-creation-requests/[a-f0-9]{32}/cancel", scope["path"]):
            limit = 4096
        if scope["path"].endswith("/knowledge/uploads"):
            limit = 1_400_000
        if "/assistant/conversations/" in scope["path"] and scope["path"].endswith("/attachments"):
            limit = 1_400_000
        size = 0
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                await JSONResponse({"detail": "Request exceeds its upload limit"}, status_code=413)(
                    scope, receive, send
                )
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
