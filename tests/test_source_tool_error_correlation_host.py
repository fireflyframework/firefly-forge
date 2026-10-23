# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Known source MCP JSONRPC IDs stay correlated when strict arguments refuse a call."""

import time

import httpx
import pytest

from firefly_forge.coding_check_transport import CodingCheckServer


async def response(packet, *, source=True):
    calls = []

    async def callback(request):
        calls.append(request)
        return {"public_fixture": True}

    kwargs = {"source_callback": callback} if source else {}
    server = CodingCheckServer(None if source else callback, expires_at_ms=int(time.time() * 1000) + 10000, **kwargs)
    app = server.application(19433)
    headers = {
        "authorization": "Bearer " + server.token,
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2025-11-25",
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:19433") as client,
    ):
        result = await client.post("/mcp", headers=headers, json=packet)
    return result, calls


def packet(identity=171):
    return {
        "jsonrpc": "2.0",
        "id": identity,
        "method": "tools/call",
        "params": {"name": "list_files", "arguments": {"request_id": "a" * 31}, "_meta": {"progressToken": 1}},
    }


@pytest.mark.parametrize(
    "params",
    [
        {"name": "list_files", "arguments": {"request_id": "a" * 31}},
        {"name": "read_file", "arguments": {"request_id": "a" * 32, "path": 1}},
        {"name": "search_text", "arguments": {"request_id": "a" * 32, "text": "public", "max_results": True}},
        {"name": "list_files", "arguments": {"request_id": "a" * 32, "argv": "PUBLIC_NEVER_ECHO"}},
        {"name": "exec_command", "arguments": {"cmd": "PUBLIC_NEVER_EXECUTE"}},
        {"name": "list_files"},
        {"name": "list_files", "arguments": []},
    ],
)
async def test_source_argument_refusal_correlates_id_without_callback(params):
    result, calls = await response(packet() | {"params": params})
    assert result.status_code == 400
    assert result.json() == {
        "jsonrpc": "2.0",
        "id": 171,
        "error": {"code": -32602, "message": "Invalid configured check"},
    }
    assert calls == []
    assert "PUBLIC_NEVER" not in result.text


@pytest.mark.parametrize("identity", [0, "public-request", "", True, None, 1.5, [], {}])
async def test_only_protocol_integer_or_string_ids_can_be_correlated(identity):
    result, calls = await response(packet(identity))
    expected = identity if type(identity) in (int, str) else None
    assert result.status_code == 400 and result.json()["id"] == expected
    assert type(result.json()["id"]) is type(expected)
    assert calls == []


@pytest.mark.parametrize("version", [None, "1.0", 2])
async def test_unrecognized_jsonrpc_version_keeps_unknown_error_id(version):
    result, calls = await response(packet() | {"jsonrpc": version})
    assert result.status_code == 400 and result.json()["id"] is None and calls == []


async def test_ordinary_configured_check_refusal_keeps_existing_envelope():
    request = packet()
    request["params"] = {"name": "run_configured_check", "arguments": {"request_id": "a" * 31, "command_index": 0}}
    result, calls = await response(request, source=False)
    assert result.status_code == 400 and result.json()["id"] is None and calls == []


async def test_valid_source_request_still_invokes_once_with_matching_id():
    request = packet()
    request["params"]["arguments"]["request_id"] = "a" * 32
    result, calls = await response(request)
    assert result.status_code == 200 and result.json()["id"] == 171
    assert not result.json()["result"].get("isError") and len(calls) == 1
