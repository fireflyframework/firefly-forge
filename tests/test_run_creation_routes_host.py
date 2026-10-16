# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Offline route classification and exact response bytes; no ASGI runtime claim."""

import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from firefly_forge.access_routes import AccessRoutes
from firefly_forge.project_access import AccessDenied
from firefly_forge.request_limits import RequestLimits

ID = "e" * 32


@pytest.mark.parametrize("publish", [False, True])
async def test_creation_route_authorizes_nested_project_and_repository(publish):
    calls = []

    async def administrator(actor):
        return False

    async def project(actor, value, permission):
        calls.append(("project", value, permission))

    async def repository(actor, value, permission):
        calls.append(("repository", value, permission))

    body = {"version": 1, "input": {"project_id": "b" * 32, "repository_id": "a" * 32, "publish": publish}}

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}

    request = Request(
        {"type": "http", "headers": [], "method": "POST", "path": "/api/v1/run-creation-requests/" + ID}, receive
    )
    access = SimpleNamespace(
        current_administrator=administrator, require_project=project, require_repository=repository
    )
    await AccessRoutes(access, SimpleNamespace()).authorize(SimpleNamespace(), request)
    assert ("project", "b" * 32, "execute") in calls
    assert ("repository", "a" * 32, "execute") in calls
    assert (("project", "b" * 32, "release") in calls) is publish
    assert (("repository", "a" * 32, "release") in calls) is publish


async def test_creation_route_does_not_accept_top_level_scope_instead_of_input():
    async def administrator(actor):
        return False

    async def receive():
        return {"type": "http.request", "body": json.dumps({"project_id": "b" * 32, "input": {}}).encode()}

    request = Request(
        {"type": "http", "headers": [], "method": "POST", "path": "/api/v1/run-creation-requests/" + ID}, receive
    )
    with pytest.raises(AccessDenied):
        await AccessRoutes(SimpleNamespace(current_administrator=administrator), SimpleNamespace()).authorize(
            SimpleNamespace(), request
        )


async def test_creation_controllers_return_canonical_identical_original_receipt_bytes():
    from firefly_forge.run_creation_controllers import RunCreationController
    from firefly_forge.run_creation_models import RunCreationInput

    value = {"version": 1, "request_id": ID, "input": {"brief": "Fictional", "repository_id": "a" * 32}}
    calls = []

    async def admit(request_id, body):
        calls.append((request_id, body))
        return value, len(calls) == 1

    async def get(request_id):
        return dict(reversed(list(value.items())))

    controller = RunCreationController(SimpleNamespace(admit=admit, get=get))
    body = RunCreationInput.model_validate({"version": 1, "input": value["input"]})
    first = await controller.create(ID, body)
    repeated = await controller.create(ID, body)
    read = await controller.get(ID)
    assert (first.status_code, repeated.status_code, read.status_code) == (201, 200, 200)
    assert first.body == repeated.body == read.body


@pytest.mark.parametrize(
    "path,accepted",
    [
        ("/api/v1/run-creation-requests/" + ID, True),
        ("/api/v1/runs", False),
        ("/api/v1/run-creation-requests/" + ID + "/extra", False),
    ],
)
async def test_new_envelope_limit_does_not_widen_legacy_or_unclassified_routes(path, accepted):
    events = []

    async def downstream(scope, receive, send):
        message = await receive()
        assert len(message["body"]) == 110_000
        events.append("accepted")

    async def receive():
        return {"type": "http.request", "body": b"x" * 110_000}

    async def send(message):
        events.append(message.get("status"))

    await RequestLimits(downstream)({"type": "http", "path": path}, receive, send)
    assert ("accepted" in events) is accepted
    assert (413 in events) is not accepted
