# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual runner controller handlers and ASGI limits with controlled authority."""

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from starlette.requests import Request
from starlette.responses import JSONResponse
from test_runner_object_artifacts_host import fixture, stream

from firefly_forge.runner_controller import (
    PoolController,
    PoolObjectRead,
    PoolObjectUpload,
    RunnerControllerService,
    RunnerRequestLimits,
    invocation_scope,
)
from firefly_forge.runner_pool_artifacts import RunnerPoolArtifacts
from firefly_forge.runner_pool_models import FactoryFence

TOKEN = "controlled-runner-token-for-host-tests-only"


def controller(tmp_path):
    objects, profile, invocation, ledger = fixture(tmp_path)
    state = SimpleNamespace(valid=True, checks=0)

    async def check(fence):
        state.checks += 1
        if not state.valid:
            raise PermissionError("Current factory fence lost")

    async def observe(_profile, _execution):
        return {"id": "1" * 32, "request": invocation.model_dump(mode="json"), "receipt": {}}

    async def authorize(_profile, _id, owner, epoch, fence):
        assert owner == "2" * 32 and epoch == 1
        await check(fence)

    pools = SimpleNamespace(check_factory_fence=check, observe=observe, authorize=authorize)
    settings = SimpleNamespace(authority=SimpleNamespace(profile=profile), token=SecretStr(TOKEN), active=True)
    artifacts = RunnerPoolArtifacts(objects.scratch, objects)
    service = RunnerControllerService(settings, pools, artifacts, None, SimpleNamespace(stopping=False))
    service.ready = True
    fence = FactoryFence(factory_schema="test_factory", pid=100, backend_started_at=datetime(2026, 10, 30, tzinfo=UTC))
    return service, PoolController(service), invocation, fence, state, ledger


async def test_raw_stream_reaches_handler_without_complete_body_buffer_and_publishes_no_execution(tmp_path):
    service, routes, invocation, fence, state, ledger = controller(tmp_path)
    metadata = PoolObjectUpload(
        invocation=invocation, fence=fence, kind="source", bytes=6, sha256=invocation.archive_digest
    )
    received, replies, result = [], [], {}
    frames = iter([b"so", b"ur", b"ce"])

    async def receive():
        data = next(frames)
        received.append(data)
        return {"type": "http.request", "body": data, "more_body": len(received) < 3}

    async def app(scope, inbound, send):
        assert received == []  # The legacy middleware had already buffered all frames.
        result.update(await routes.upload_object(Request(scope, inbound)))
        await JSONResponse(result)(scope, inbound, send)

    async def send(message):
        replies.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/runner/v1/object-artifacts/upload",
        "scheme": "https",
        "headers": [
            (b"authorization", ("Bearer " + TOKEN).encode()),
            (b"x-forge-artifact-request", metadata.model_dump_json().encode()),
        ],
        "query_string": b"",
        "server": ("test", 443),
        "client": ("test", 1),
        "http_version": "1.1",
    }
    await RunnerRequestLimits(app, service.settings)(scope, receive, send)
    assert received == [b"so", b"ur", b"ce"] and replies[0]["status"] == 200
    assert result["execution_granted"] is False and state.checks > 0
    reference = result["reference"]
    assert reference["sha256"] == hashlib.sha256(b"source").hexdigest()
    lookup = PoolObjectRead.model_validate(
        {
            "execution_id": invocation.execution_id,
            "scope": invocation_scope(service.settings.token, invocation.model_dump(mode="json")),
            "reference": reference,
            "lease": {"owner_id": "2" * 32, "epoch": 1, "fence": fence.model_dump(mode="json")},
        }
    )
    assert b"".join([part async for part in service.stream_reference(lookup)]) == b"source"
    assert len(ledger.rows) == 1 and not service.executions.stopping


@pytest.mark.parametrize("fault", ["inactive", "ownership", "digest", "scope"])
async def test_object_upload_or_read_rechecks_current_authority_and_frozen_identity(tmp_path, fault):
    service, _, invocation, fence, state, ledger = controller(tmp_path)
    request = PoolObjectUpload(
        invocation=invocation, fence=fence, kind="source", bytes=6, sha256=invocation.archive_digest
    )
    if fault == "inactive":
        service.ready = False
    elif fault == "ownership":
        state.valid = False
    elif fault == "digest":
        request = request.model_copy(update={"sha256": "a" * 64})
    if fault == "scope":
        lookup = PoolObjectRead.model_validate(
            {
                "execution_id": invocation.execution_id,
                "scope": "0" * 64,
                "lease": {"owner_id": "2" * 32, "epoch": 1, "fence": fence.model_dump(mode="json")},
                "reference": service.artifacts.objects.reference(
                    invocation,
                    "source",
                    {
                        "kind": "source",
                        "bytes": 6,
                        "sha256": invocation.archive_digest,
                    },
                ).model_dump(mode="json"),
            }
        )
        with pytest.raises(PermissionError):
            _ = [part async for part in service.stream_reference(lookup)]
    else:
        with pytest.raises((RuntimeError, PermissionError, ValueError)):
            await service.upload_object(request, stream(b"source"))
    assert ledger.rows == {}


async def test_stream_endpoint_rejects_bad_metadata_before_reading_body(tmp_path):
    _, routes, _, _, _, ledger = controller(tmp_path)

    async def receive():
        pytest.fail("Invalid metadata must not consume the artifact body")

    request = Request({"type": "http", "headers": [(b"x-forge-artifact-request", b"{}")]}, receive)
    with pytest.raises(ValueError):
        await routes.upload_object(request)
    assert ledger.rows == {}
