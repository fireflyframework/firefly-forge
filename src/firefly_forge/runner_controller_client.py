# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded mutually authenticated worker RPC; uncertain requests are never retried."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from typing import Any

import httpx
from pyfly.container import service

from firefly_forge.runner_controller import invocation_scope
from firefly_forge.runner_controller_config import RunnerWorkerTransport
from firefly_forge.runner_pool_artifacts import validate_artifact_receipt
from firefly_forge.runner_pool_models import FactoryFence, RunnerInvocation, pool_digest

RPC_DEADLINE_SECONDS = 30


@service
class RunnerControllerClient:
    def __init__(self, transport: RunnerWorkerTransport) -> None:
        self.transport = transport

    async def _request(
        self, path: str, payload: dict[str, Any] | None = None, *, limit: int = 1_048_576
    ) -> dict[str, Any]:
        self.transport.require_active()
        async with (
            asyncio.timeout(RPC_DEADLINE_SECONDS),
            httpx.AsyncClient(
                verify=self.transport.client_context(),
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(30, connect=5),
                limits=httpx.Limits(max_connections=1),
            ) as client,
            client.stream(
                "POST" if payload is not None else "GET",
                self.transport.endpoint + "/runner/v1/" + path,
                json=payload,
                headers={"Authorization": "Bearer " + self.transport.token.get_secret_value()},
            ) as response,
        ):
            output = bytearray()
            async for chunk in response.aiter_bytes():
                if len(output) + len(chunk) > limit:
                    raise RuntimeError("Runner RPC response exceeds its byte limit")
                output.extend(chunk)
            if response.status_code != 200:
                raise RuntimeError("Runner RPC refused the request; reconcile without automatic replay")
            value = json.loads(output)
            if not isinstance(value, dict):
                raise RuntimeError("Runner RPC response is not a typed object")
            return value

    async def profile(self) -> dict[str, Any]:
        value = await self._request("profile")
        profile = value.get("profile", {})
        if (
            not isinstance(profile, dict)
            or profile.get("profile_id") != self.transport.profile_id
            or value.get("revision") != self.transport.profile_revision
            or pool_digest(profile) != self.transport.profile_revision
        ):
            raise RuntimeError("Controller profile differs from frozen worker authority")
        return value

    def _scope(self, invocation: RunnerInvocation) -> dict[str, str]:
        if (
            invocation.profile_id != self.transport.profile_id
            or invocation.profile_revision != self.transport.profile_revision
        ):
            raise ValueError("Invocation differs from frozen worker profile")
        return {
            "execution_id": invocation.execution_id,
            "scope": invocation_scope(self.transport.token, invocation.model_dump(mode="json")),
        }

    async def submit(
        self, invocation: RunnerInvocation, fence: FactoryFence, archive: bytes, stdin: bytes = b""
    ) -> dict[str, Any]:
        self._scope(invocation)
        if len(archive) != invocation.source_bytes or hashlib.sha256(archive).hexdigest() != invocation.archive_digest:
            raise ValueError("Worker archive differs from immutable invocation")
        if len(stdin) != invocation.stdin_bytes or hashlib.sha256(stdin).hexdigest() != (
            invocation.stdin_digest or hashlib.sha256(b"").hexdigest()
        ):
            raise ValueError("Worker stdin differs from immutable invocation")
        return await self._request(
            "invocations",
            {
                "invocation": invocation.model_dump(mode="json"),
                "fence": fence.model_dump(mode="json"),
                "archive_base64": base64.b64encode(archive).decode(),
                "stdin_base64": base64.b64encode(stdin).decode(),
            },
        )

    async def observe(self, invocation: RunnerInvocation) -> dict[str, Any]:
        value = await self._request("observe", self._scope(invocation))
        if value.get("request") != invocation.model_dump(mode="json"):
            raise RuntimeError("Observed invocation differs from frozen worker request")
        return value

    async def continue_invocation(
        self, invocation: RunnerInvocation, fence: FactoryFence, request_id: str, request_digest: str
    ) -> dict[str, Any]:
        return await self._request(
            "continue",
            {
                **self._scope(invocation),
                "fence": fence.model_dump(mode="json"),
                "request_id": request_id,
                "request_digest": request_digest,
            },
        )

    async def reconcile(self, invocation: RunnerInvocation, fence: FactoryFence) -> dict[str, Any]:
        return await self._request("reconcile", {**self._scope(invocation), "fence": fence.model_dump(mode="json")})

    async def cancel(self, invocation: RunnerInvocation, fence: FactoryFence) -> dict[str, Any]:
        return await self._request("cancel", {**self._scope(invocation), "fence": fence.model_dump(mode="json")})

    async def artifact(self, invocation: RunnerInvocation, kind: str, receipt: dict[str, Any]) -> bytes:
        limit = min(invocation.max_output_bytes, 269_484_032) if kind == "output" else 269_484_032
        receipt = validate_artifact_receipt(kind, receipt, limit)
        size = receipt["bytes"]
        value = await self._request(
            "artifact", {**self._scope(invocation), "kind": kind}, limit=4 * ((size + 2) // 3) + 4096
        )
        if value.get("receipt") != receipt:
            raise RuntimeError("Controller artifact receipt changed")
        data = base64.b64decode(value["data_base64"], validate=True)
        if len(data) != size or hashlib.sha256(data).hexdigest() != receipt.get("sha256"):
            raise RuntimeError("Returned artifact differs from immutable receipt")
        return data
