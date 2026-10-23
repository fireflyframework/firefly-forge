# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The source channel has no execution callback even if client allowlists are wrong."""

import builtins
import io
import os
import socket
import time

import httpx

from firefly_forge.coding_check_transport import CodingCheckServer


async def test_source_channel_lists_only_three_readers_and_rejects_execution_arguments():
    calls = []

    async def read(request):
        calls.append(request.model_dump())
        return {"source_digest": "a" * 64, "content": "public source"}

    server = CodingCheckServer(None, expires_at_ms=int(time.time() * 1000) + 30000, source_callback=read)
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
        listing = await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert {tool["name"] for tool in listing.json()["result"]["tools"]} == {
            "list_files",
            "read_file",
            "search_text",
        }
        schemas = {tool["name"]: tool["inputSchema"] for tool in listing.json()["result"]["tools"]}
        path = schemas["read_file"]["properties"]["path"]
        assert path["minLength"] == 1 and path["maxLength"] == 1000
        search = schemas["search_text"]["properties"]
        assert search["text"]["minLength"] == 1 and search["text"]["maxLength"] == 500
        assert search["path"]["maxLength"] == 1000
        assert search["max_results"]["minimum"] == 1 and search["max_results"]["maximum"] == 100
        packet = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"request_id": "a" * 32, "path": "src/app.py"}},
        }
        good = await client.post("/mcp", headers=headers, json=packet)
        assert not good.json()["result"].get("isError")
        for params in (
            {"name": "run_configured_check", "arguments": {"command_index": 0, "request_id": "b" * 32}},
            {"name": "exec_command", "arguments": {"cmd": "touch forbidden"}},
            {"name": "read_file", "arguments": {"request_id": "b" * 32, "path": "src/app.py", "argv": ["shell"]}},
        ):
            bad = await client.post("/mcp", headers=headers, json=packet | {"params": params})
            assert bad.status_code == 400
        assert len(calls) == 1


async def test_source_resources_are_empty_and_never_open_file_or_network(tmp_path, monkeypatch):
    marker = tmp_path / "not-a-source-resource.txt"
    marker.write_text("PRIVATE_RESOURCE_SENTINEL_DO_NOT_RETURN")
    attempts = []
    source_calls = []

    async def read(request):
        source_calls.append(request)
        raise AssertionError("Resource requests cannot invoke the governed source readers")

    server = CodingCheckServer(None, expires_at_ms=int(time.time() * 1000) + 30000, source_callback=read)
    app = server.application(19433)
    headers = {
        "authorization": "Bearer " + server.token,
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2025-11-25",
    }
    original_open, original_io_open, original_os_open = builtins.open, io.open, os.open

    def file_guard(original):
        def guarded(path, *args, **kwargs):
            if str(path) == str(marker):
                attempts.append("file")
                raise AssertionError("Resource URI attempted a file open")
            return original(path, *args, **kwargs)

        return guarded

    def no_network(*args, **kwargs):
        attempts.append("network")
        raise AssertionError("Resource URI attempted network access")

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:19433") as client,
    ):
        monkeypatch.setattr(builtins, "open", file_guard(original_open))
        monkeypatch.setattr(io, "open", file_guard(original_io_open))
        monkeypatch.setattr(os, "open", file_guard(original_os_open))
        monkeypatch.setattr(socket, "getaddrinfo", no_network)
        monkeypatch.setattr(socket.socket, "connect", no_network)
        for method, expected in (
            ("resources/list", {"resources": []}),
            ("resources/templates/list", {"resourceTemplates": []}),
        ):
            response = await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method})
            assert response.json()["result"] == expected
        for uri in (marker.as_uri(), "https://resource-never-opened.invalid/private"):
            response = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "resources/read",
                    "params": {"uri": uri, "_meta": {"progressToken": 123}},
                },
            )
            assert "error" in response.json() and "result" not in response.json()
            assert "PRIVATE_RESOURCE_SENTINEL_DO_NOT_RETURN" not in response.text
        for method in ("resources/list", "resources/templates/list", "resources/read"):
            params = {"server": "unregistered"}
            if method == "resources/read":
                params["uri"] = marker.as_uri()
            response = await client.post(
                "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": method, "params": params}
            )
            assert response.status_code == 400
        assert attempts == [] and source_calls == []
