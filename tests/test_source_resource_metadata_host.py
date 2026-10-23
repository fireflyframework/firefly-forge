# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real scoped MCP resource metadata, strict refusal, and JSON-RPC correlation."""

import time
from contextlib import asynccontextmanager

import httpx
import pytest

from firefly_forge.coding_check_transport import CodingCheckServer


@asynccontextmanager
async def resource_client():
    calls = []

    async def read(request):
        calls.append(request)
        raise AssertionError("Inert resources cannot invoke a source reader")

    server = CodingCheckServer(None, expires_at_ms=int(time.time() * 1000) + 30000, source_callback=read)
    app = server.application(19433)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://127.0.0.1:19433",
            headers={
                "authorization": "Bearer " + server.token,
                "accept": "application/json, text/event-stream",
                "mcp-protocol-version": "2025-11-25",
            },
        ) as client,
    ):
        yield client, calls
    assert calls == []


@pytest.mark.parametrize(
    "method,field", [("resources/list", "resources"), ("resources/templates/list", "resourceTemplates")]
)
@pytest.mark.parametrize(
    "params",
    [
        {"_meta": {"progressToken": 123}},
        {"_meta": {"progressToken": "public-token"}, "cursor": None},
        {"_meta": None},
        {"cursor": None},
        {"cursor": "public-cursor", "_meta": {}},
    ],
)
async def test_standard_resource_metadata_remains_empty_with_correlated_response(method, field, params):
    async with resource_client() as (client, calls):
        response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 123, "method": method, "params": params})
        assert response.status_code == 200
        assert response.json() == {"jsonrpc": "2.0", "id": 123, "result": {field: []}}
        assert calls == []


@pytest.mark.parametrize(
    "params",
    [
        {"server": "unregistered", "_meta": {"progressToken": 1}},
        {"uri": "file:///public-denied", "_meta": {"progressToken": 1}},
        {"_meta": {"server": "unregistered"}},
        {"_meta": {"progressToken": True}},
        {"_meta": {"progressToken": 1.5}},
        {"_meta": {"progressToken": None}},
        {"_meta": {"progressToken": []}},
        {"_meta": {"progressToken": {}}},
        {"_meta": {"progressToken": "x" * 2049}},
        {"_meta": []},
        {"_meta": "x"},
        {"cursor": True},
        {"cursor": 1},
        {"cursor": []},
        {"cursor": "x" * 2049},
    ],
)
async def test_unknown_fields_and_invalid_metadata_are_refused_with_original_request_id(params):
    async with resource_client() as (client, calls):
        response = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": "public-denied-id", "method": "resources/list", "params": params}
        )
        assert response.status_code == 400
        assert response.json()["id"] == "public-denied-id"
        assert response.json()["error"]["code"] == -32602
        assert calls == []


@pytest.mark.parametrize("uri", [None, "", True, "x" * 2049])
async def test_metadata_does_not_relax_resource_uri(uri):
    async with resource_client() as (client, calls):
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "resources/read",
                "params": {"uri": uri, "_meta": {"progressToken": 1}},
            },
        )
        assert response.status_code == 400
        assert response.json()["id"] == 7
        assert calls == []


@pytest.mark.parametrize("request_id", [True, 1.5, [], {}, None])
async def test_invalid_jsonrpc_id_is_never_reflected(request_id):
    async with resource_client() as (client, calls):
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": request_id, "method": "resources/list", "params": {"server": "unregistered"}},
        )
        assert response.status_code == 400
        assert response.json()["id"] is None
        assert calls == []


async def test_metadata_does_not_admit_execution_tool_or_extra_tool_arguments():
    async with resource_client() as (client, calls):
        for params in (
            {"name": "exec_command", "arguments": {"cmd": "public-never-executed"}},
            {
                "name": "read_file",
                "arguments": {"path": "src/app.py", "request_id": "a" * 32, "argv": ["public-never-executed"]},
            },
        ):
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": params | {"_meta": {"progressToken": 1}},
                },
            )
            assert response.status_code == 400
            assert response.json()["id"] is None
        assert calls == []
