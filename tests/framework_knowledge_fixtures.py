# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Controlled real-SDK HTTP fixture; no native storage or authority substitutions."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from test_framework_knowledge_upstream_host import official_fixture as official_fixture


class ControlledFlycanon:
    def __init__(self) -> None:
        self.sources: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.posts: list[dict[str, Any]] = []
        self.before_post: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self.unknown_before_create = False
        self.unknown_after_create = False
        self.fail_retrieval = False
        self.before_search: Callable[[], Awaitable[None]] | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        payload = json.loads(request.content) if request.content else {}
        workspace = request.headers["X-Workspace-Id"]
        tenant = request.headers["X-Tenant-Id"]
        timestamp = "2026-10-26T00:00:00Z"
        assert workspace.startswith("ffw-") or workspace.startswith("forge-")
        if path.startswith("/api/v1/workspaces/"):
            return httpx.Response(
                200,
                json={
                    "id": workspace,
                    "tenant_id": tenant,
                    "name": "Official fixture",
                    "status": "active",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
        if path == "/api/v1/sources" and request.method == "POST":
            if self.before_post:
                await self.before_post(payload)
            self.posts.append(payload)
            if self.unknown_before_create:
                raise httpx.ReadTimeout("Controlled unknown source outcome", request=request)
            body = base64.b64decode(payload["content_base64"])
            identity = "source-" + str(len(self.sources) + 1)
            record = {
                "id": identity,
                "kind": "text",
                "status": "ingested",
                "filename": payload["filename"],
                "content_sha256": hashlib.sha256(body).hexdigest(),
                "content_bytes": len(body),
                "n_chunks": 1,
                "metadata": payload["metadata"],
                "created_at": timestamp,
                "updated_at": timestamp,
                "body": body.decode(),
                "workspace": workspace,
            }
            self.sources[identity] = record
            if self.unknown_after_create:
                raise httpx.ReadTimeout("Controlled unknown source outcome", request=request)
            return httpx.Response(201, json=record)
        if path == "/api/v1/sources":
            offset = int(request.url.params.get("offset", "0"))
            limit = int(request.url.params.get("limit", "50"))
            values = [v for v in self.sources.values() if v["workspace"] == workspace]
            return httpx.Response(
                200,
                json={"items": values[offset : offset + limit], "total": len(values), "offset": offset, "limit": limit},
            )
        if path.startswith("/api/v1/sources/"):
            record = self.sources[path.rsplit("/", 1)[1]]
            assert record["workspace"] == workspace
            return httpx.Response(200, json=record)
        if path == "/api/v1/search":
            if self.before_search:
                await self.before_search()
            ids = (
                payload.get("source_ids")
                or [key for key, value in self.sources.items() if value["workspace"] == workspace][:1]
            )
            assert all(self.sources[key]["workspace"] == workspace for key in ids)
            return httpx.Response(
                200,
                json={
                    "elapsed_ms": 1,
                    "hits": []
                    if self.fail_retrieval
                    else [
                        {
                            "chunk_id": "chunk-" + key,
                            "source_id": key,
                            "source_filename": self.sources[key]["filename"],
                            "content": self.sources[key]["body"],
                            "score": 0.9,
                            "knowledge_version": None,
                        }
                        for key in ids
                    ],
                },
            )
        raise AssertionError("Unexpected controlled Flycanon request " + path)
