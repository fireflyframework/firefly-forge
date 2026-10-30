# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Bounded mutually authenticated worker RPC; uncertain requests are never retried."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from pyfly.container import service

from firefly_forge.runner_controller import PoolObjectUpload, invocation_scope
from firefly_forge.runner_controller_config import RunnerWorkerTransport
from firefly_forge.runner_object_artifacts import RunnerArtifactReadLease, RunnerObjectReference
from firefly_forge.runner_pool_artifacts import validate_artifact_receipt
from firefly_forge.runner_pool_models import FactoryFence, RunnerInvocation, pool_digest
from firefly_forge.runner_run_liveness import RunLeaseAckV1, RunRenewalV1, validate_run_ack

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

    async def renew_run(
        self, invocation: RunnerInvocation, fence: FactoryFence, previous: dict[str, Any]
    ) -> dict[str, Any]:
        if invocation.run_liveness is None:
            raise ValueError("Run renewal requires an explicit frozen ownership binding")
        before = RunLeaseAckV1.model_validate(previous)
        identity = pool_digest(invocation.model_dump(mode="json"))
        validate_run_ack(previous, identity, invocation.run_liveness, sequence=before.sequence)
        renewal = RunRenewalV1.model_validate(
            {**previous, "protocol": "forge-run-renewal-v1", "sequence": before.sequence + 1}
        )
        value = await self._request(
            "renew-run",
            {
                **self._scope(invocation),
                "fence": fence.model_dump(mode="json"),
                "renewal": renewal.model_dump(mode="json"),
            },
        )
        return validate_run_ack(
            value,
            identity,
            invocation.run_liveness,
            sequence=before.sequence + 1,
            controller_epoch=before.controller_epoch,
        )

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

    async def upload_artifact_file(
        self, invocation: RunnerInvocation, fence: FactoryFence, kind: str, receipt: dict[str, Any], source: Path
    ) -> RunnerObjectReference:
        """Stage a file by bounded reads; this request cannot submit a command."""
        self.transport.require_active()
        self._scope(invocation)
        if receipt.get("kind") != kind or set(receipt) != {"kind", "bytes", "sha256"}:
            raise ValueError("Artifact upload requires its exact typed receipt")
        request = PoolObjectUpload.model_validate(
            {
                "invocation": invocation,
                "fence": fence,
                "kind": kind,
                "bytes": receipt["bytes"],
                "sha256": receipt["sha256"],
            }
        )

        async def content() -> AsyncIterator[bytes]:
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as file:
                before = os.fstat(file.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1:
                    raise ValueError("Artifact producer requires its owned regular runner-volume file")
                if before.st_size != receipt["bytes"]:
                    raise ValueError("Artifact file differs from its immutable receipt")
                while data := file.read(65_536):
                    yield data
                after = os.fstat(file.fileno())
                fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_nlink")
                if any(getattr(before, name) != getattr(after, name) for name in fields):
                    raise RuntimeError("Artifact file changed while streaming; explicit readback is required")

        metadata = json.dumps(request.model_dump(mode="json"), ensure_ascii=True, separators=(",", ":"))
        if len(metadata) > 65_536:
            raise ValueError("Artifact metadata exceeds its transport header envelope")
        async with (
            asyncio.timeout(RPC_DEADLINE_SECONDS),
            httpx.AsyncClient(
                verify=self.transport.client_context(),
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(30, connect=5),
            ) as client,
            client.stream(
                "POST",
                self.transport.endpoint + "/runner/v1/object-artifacts/upload",
                content=content(),
                headers={
                    "Authorization": "Bearer " + self.transport.token.get_secret_value(),
                    "X-Forge-Artifact-Request": metadata,
                },
            ) as response,
        ):
            output = bytearray()
            async for data in response.aiter_bytes():
                if len(output) + len(data) > 1_048_576:
                    raise RuntimeError("Artifact publication response exceeds its metadata envelope")
                output.extend(data)
            if response.status_code != 200:
                raise RuntimeError("Artifact upload outcome requires explicit readback; no automatic retry")
            value = json.loads(output)
        if (
            not isinstance(value, dict)
            or set(value) != {"protocol", "receipt", "reference", "execution_granted"}
            or value["protocol"] != "forge-object-artifact-v1"
            or value["receipt"] != receipt
            or value["execution_granted"] is not False
        ):
            raise RuntimeError("Artifact upload response differs from its exact data-only request")
        reference = RunnerObjectReference.model_validate(value["reference"])
        if (
            reference.execution_id != invocation.execution_id
            or reference.invocation_digest != pool_digest(invocation.model_dump(mode="json"))
            or reference.receipt() != receipt
        ):
            raise RuntimeError("Artifact reference differs from its immutable invocation and receipt")
        return reference

    async def read_object(
        self, invocation: RunnerInvocation, reference: RunnerObjectReference, lease: RunnerArtifactReadLease
    ) -> AsyncIterator[bytes]:
        self.transport.require_active()
        payload = {
            **self._scope(invocation),
            "reference": reference.model_dump(mode="json"),
            "lease": lease.model_dump(mode="json"),
        }
        if reference.execution_id != invocation.execution_id or reference.invocation_digest != pool_digest(
            invocation.model_dump(mode="json")
        ):
            raise ValueError("Artifact reference belongs to another invocation")
        reference.receipt()
        size, digest = 0, hashlib.sha256()
        async with (
            asyncio.timeout(RPC_DEADLINE_SECONDS),
            httpx.AsyncClient(
                verify=self.transport.client_context(),
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(30, connect=5),
            ) as client,
            client.stream(
                "POST",
                self.transport.endpoint + "/runner/v1/object-artifacts/read-reference",
                json=payload,
                headers={"Authorization": "Bearer " + self.transport.token.get_secret_value()},
            ) as response,
        ):
            if response.status_code != 200:
                raise RuntimeError("Artifact reference read was refused")
            async for data in response.aiter_bytes(chunk_size=65_536):
                size += len(data)
                if size > reference.bytes:
                    raise RuntimeError("Artifact stream exceeds its immutable receipt")
                digest.update(data)
                yield data
        if size != reference.bytes or digest.hexdigest() != reference.sha256:
            raise RuntimeError("Artifact stream differs from its immutable receipt")
