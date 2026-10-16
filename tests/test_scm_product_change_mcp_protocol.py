# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual in-memory MCP sessions through the registered SDK-backed operator tools."""

import contextlib
import json

import httpx
from mcp import Client
from test_scm_product_change_clients import BASE, BINDING, PROJECT, REPOSITORY, REQUEST, connection_with, grant

from firefly_forge import cli
from firefly_forge.mcp_server import create_server


async def test_product_edit_adapters_through_initialized_mcp_session(monkeypatch):
    calls = []
    body = grant()
    receipt = {"version": 1, "request": body, "digest": "3" * 64}

    def transport(request):
        calls.append(request)
        return httpx.Response(201 if request.method == "POST" else 200, json=receipt)

    with connection_with(transport) as connection:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        async with Client(create_server(), cache=None, read_timeout_seconds=5) as session:
            catalog = await session.list_tools()
            names = {tool.name for tool in catalog.tools}
            operations = [
                ("configure_scm_product_change_grant", {"request": body}),
                ("get_scm_product_change_grant", {"request_id": REQUEST}),
                ("get_current_scm_product_change_grant", {"binding_request_id": BINDING}),
                ("get_scm_product_change", {"request_id": REQUEST}),
            ]
            assert {name for name, _ in operations} <= names
            for name, kwargs in operations:
                result = await session.call_tool(name, {"project_id": PROJECT, "repository_id": REPOSITORY, **kwargs})
                assert not result.is_error
                assert result.structured_content == receipt
    assert [(r.method, r.url.path) for r in calls] == [
        ("POST", "/api/v1/" + BASE),
        ("GET", "/api/v1/" + BASE + "/requests/" + REQUEST),
        ("GET", "/api/v1/" + BASE + "/current/" + BINDING),
        ("GET", f"/api/v1/projects/{PROJECT}/repositories/{REPOSITORY}/scm-product-changes/requests/{REQUEST}"),
    ]
    assert json.loads(calls[0].content) == body


async def test_protocol_invalid_consent_cannot_reach_http(monkeypatch):
    calls = []

    def transport(request):
        calls.append(request)
        raise AssertionError("Invalid consent must not reach HTTP")

    body = {**grant(), "active": 1}
    with connection_with(transport) as connection:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        async with Client(create_server(), cache=None, read_timeout_seconds=5) as session:
            result = await session.call_tool(
                "configure_scm_product_change_grant",
                {"project_id": PROJECT, "repository_id": REPOSITORY, "request": body},
            )
            assert result.is_error
    assert calls == []
