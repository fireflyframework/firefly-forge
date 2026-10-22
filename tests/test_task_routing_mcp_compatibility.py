# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The original routing tool keeps its frozen input and HTTP payload."""

import contextlib
import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server
from firefly_forge.task_routing import TaskRoutingInput


@pytest.mark.parametrize("bindings", [{}, {"communication": "updates"}])
async def test_original_mcp_routing_sends_only_the_original_fields(monkeypatch, bindings):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"bindings": bindings, "revision": 1})

    with ForgeClient("https://factory.invalid", "fictional-routing-token") as connection:
        connection.http.close()
        connection.http = httpx.Client(
            base_url="https://factory.invalid/api/v1/", transport=httpx.MockTransport(transport)
        )
        monkeypatch.setattr("firefly_forge.cli.client", lambda: contextlib.nullcontext(connection))
        result = await create_server().call_tool("save_task_routing", {"request": {"bindings": bindings}})
        assert result.structured_content == {"bindings": bindings, "revision": 1}
    assert len(calls) == 1
    assert (calls[0].method, calls[0].url.path) == ("PUT", "/api/v1/task-routing")
    assert json.loads(calls[0].content) == {"bindings": bindings}


@pytest.mark.parametrize("digest", [None, "a" * 64])
async def test_original_mcp_routing_rejects_unadvertised_concurrency_before_client(monkeypatch, digest):
    calls = []
    monkeypatch.setattr("firefly_forge.cli.client", lambda: calls.append("opened client"))
    with pytest.raises(ToolError):
        await create_server().call_tool("save_task_routing", {"request": {"bindings": {}, "expected_digest": digest}})
    assert calls == []


@pytest.mark.parametrize("bindings", [{"unknown": "updates"}, {"communication": "invalid profile"}])
async def test_original_mcp_routing_keeps_native_binding_validation(monkeypatch, bindings):
    calls = []
    monkeypatch.setattr("firefly_forge.cli.client", lambda: calls.append("opened client"))
    with pytest.raises(ToolError):
        await create_server().call_tool("save_task_routing", {"request": {"bindings": bindings}})
    assert calls == []


@pytest.mark.parametrize("as_model", [False, True])
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({}, {"bindings": {}}),
        ({"bindings": {}}, {"bindings": {}}),
        ({"bindings": {"communication": "updates"}}, {"bindings": {"communication": "updates"}}),
        ({"expected_digest": None}, {"bindings": {}, "expected_digest": None}),
        (
            {"bindings": {"communication": "updates"}, "expected_digest": "a" * 64},
            {"bindings": {"communication": "updates"}, "expected_digest": "a" * 64},
        ),
    ],
)
def test_native_sdk_routing_preserves_concurrency_field_presence(as_model, body, expected):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"revision": 2})

    with ForgeClient("https://factory.invalid", "fictional-routing-token") as connection:
        connection.http.close()
        connection.http = httpx.Client(
            base_url="https://factory.invalid/api/v1/", transport=httpx.MockTransport(transport)
        )
        request = TaskRoutingInput.model_validate(body) if as_model else body
        assert connection.save_task_routing(request) == {"revision": 2}
    assert len(calls) == 1
    assert json.loads(calls[0].content) == expected
