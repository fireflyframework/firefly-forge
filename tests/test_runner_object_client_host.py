# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Production file producer and typed reference consumer use controlled HTTP only."""

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from test_runner_object_artifacts_host import fixture

from firefly_forge.runner_controller_client import RunnerControllerClient
from firefly_forge.runner_object_artifacts import RunnerArtifactReadLease, RunnerObjectArtifacts
from firefly_forge.runner_pool_models import FactoryFence


class ControlledStream(httpx.MockTransport):
    def __init__(self, handler):
        super().__init__(handler)
        self.handler = handler

    async def handle_async_request(self, request):
        # MockTransport normally buffers the request before its handler. This
        # controlled native seam observes the producer's actual frame sizes.
        return await self.handler(request)


def client_for(profile, monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=ControlledStream(handler), **kwargs))
    transport = SimpleNamespace(
        require_active=lambda: None,
        client_context=lambda: True,
        endpoint="https://controller.example",
        token=SecretStr("controlled-transport-token"),
        profile_id=profile.profile_id,
        profile_revision=profile.revision,
    )
    return RunnerControllerClient(transport)


def fence():
    return FactoryFence(factory_schema="factory", pid=100, backend_started_at=datetime(2026, 10, 30, tzinfo=UTC))


async def test_file_producer_streams_bounded_frames_without_read_bytes_or_base64(tmp_path, monkeypatch):
    _, profile, invocation, _ = fixture(tmp_path)
    data = b"source" * 100000
    digest = hashlib.sha256(data).hexdigest()
    invocation = invocation.model_copy(
        update={"source_bytes": len(data), "archive_digest": digest, "argv": ("echo", "café")}
    )
    source = tmp_path / "source.tar"
    source.write_bytes(data)
    receipt = {"kind": "source", "bytes": len(data), "sha256": digest}
    reference = RunnerObjectArtifacts.reference(invocation, "source", receipt)
    frames = []

    async def handler(request):
        assert request.url.path == "/runner/v1/object-artifacts/upload"
        metadata = json.loads(request.headers["x-forge-artifact-request"])
        assert "archive_base64" not in metadata and str(source) not in json.dumps(metadata)
        checksum = hashlib.sha256()
        async for part in request.stream:
            frames.append(len(part))
            checksum.update(part)
        assert checksum.hexdigest() == digest
        return httpx.Response(
            200,
            json={
                "protocol": "forge-object-artifact-v1",
                "receipt": receipt,
                "reference": reference.model_dump(mode="json"),
                "execution_granted": False,
            },
        )

    def forbidden_read_bytes(_self):
        pytest.fail("The file producer must not read the complete archive")

    monkeypatch.setattr(type(source), "read_bytes", forbidden_read_bytes)
    client = client_for(profile, monkeypatch, handler)
    assert await client.upload_artifact_file(invocation, fence(), "source", receipt, source) == reference
    assert max(frames) <= 65536 and len(frames) > 1


@pytest.mark.parametrize("fault", ["grant", "invocation", "receipt", "lost_reply"])
async def test_upload_refuses_response_drift_and_never_retries_uncertain_post(tmp_path, monkeypatch, fault):
    _, profile, invocation, _ = fixture(tmp_path)
    source = tmp_path / "source.tar"
    source.write_bytes(b"source")
    receipt = {"kind": "source", "bytes": 6, "sha256": invocation.archive_digest}
    reference = RunnerObjectArtifacts.reference(invocation, "source", receipt)
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        async for _ in request.stream:
            pass
        if fault == "lost_reply":
            raise httpx.ReadTimeout("controlled response loss")
        value = {
            "protocol": "forge-object-artifact-v1",
            "receipt": receipt,
            "reference": reference.model_dump(mode="json"),
            "execution_granted": False,
        }
        if fault == "grant":
            value["execution_granted"] = 0
        elif fault == "invocation":
            value["reference"]["invocation_digest"] = "0" * 64
        else:
            value["receipt"] = {**receipt, "sha256": "0" * 64}
        return httpx.Response(200, json=value)

    client = client_for(profile, monkeypatch, handler)
    with pytest.raises((RuntimeError, ValueError, httpx.ReadTimeout)):
        await client.upload_artifact_file(invocation, fence(), "source", receipt, source)
    assert calls == 1


async def test_reference_consumer_verifies_full_hash_and_transmits_exact_current_lease(tmp_path, monkeypatch):
    _, profile, invocation, _ = fixture(tmp_path)
    receipt = {"kind": "source", "bytes": 6, "sha256": invocation.archive_digest}
    reference = RunnerObjectArtifacts.reference(invocation, "source", receipt)
    lease = RunnerArtifactReadLease(owner_id="2" * 32, epoch=1, fence=fence())
    corrupt = False

    class Frames(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"sou"
            yield b"bad" if corrupt else b"rce"

    async def handler(request):
        body = json.loads(await request.aread())
        assert body["lease"] == lease.model_dump(mode="json")
        assert body["reference"] == reference.model_dump(mode="json")
        return httpx.Response(200, stream=Frames())

    client = client_for(profile, monkeypatch, handler)
    assert b"".join([part async for part in client.read_object(invocation, reference, lease)]) == b"source"
    corrupt = True
    with pytest.raises(RuntimeError, match="immutable receipt"):
        _ = [part async for part in client.read_object(invocation, reference, lease)]
