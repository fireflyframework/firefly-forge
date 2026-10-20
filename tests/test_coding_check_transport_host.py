# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real MCP ASGI protocol, with one public local callback and no coding provider."""

import json
import time

import httpx
import pytest


@pytest.mark.asyncio
async def test_coding_transport_exposes_only_configured_check_and_enforces_exact_boundary():
    from firefly_forge.coding_check_transport import CodingCheckServer

    calls = []

    async def callback(request):
        calls.append(request.model_dump())
        return {"exit_code": 0, "source_digest": "a" * 64}

    server = CodingCheckServer(callback, expires_at_ms=int(time.time() * 1000) + 60000)
    app = server.application(19431)
    headers = {
        "authorization": "Bearer " + server.token,
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2025-11-25",
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:19431") as client,
    ):
        response = await client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        assert response.status_code == 200, response.text
        tools = response.json()["result"]["tools"]
        assert [item["name"] for item in tools] == ["run_configured_check"]
        packet = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "run_configured_check", "arguments": {"command_index": 19, "request_id": "b" * 32}},
        }
        accepted = await client.post("/mcp", headers=headers, json=packet)
        assert accepted.status_code == 200 and not accepted.json()["result"].get("isError")
        assert calls == [{"command_index": 19, "request_id": "b" * 32}]
        for change in (
            {"name": "create_run"},
            {
                "name": "run_configured_check",
                "arguments": {"command_index": 0, "request_id": "b" * 32, "argv": ["other"]},
            },
        ):
            bad = await client.post("/mcp", headers=headers, json=packet | {"params": change})
            assert "error" in bad.json() or bad.json()["result"].get("isError") is True
        assert len(calls) == 1
        for overrides, path, status in (
            ({"authorization": "Bearer wrong"}, "/mcp", 401),
            ({"origin": "https://attacker.invalid"}, "/mcp", 403),
            ({"host": "attacker.invalid"}, "/mcp", 403),
            ({}, "/mcp?token=untrusted", 404),
            ({}, "/other", 404),
        ):
            denied = await client.post(path, headers=headers | overrides, json=packet)
            assert denied.status_code == status
            assert server.token not in denied.text
        server.accepting = False
        assert (await client.post("/mcp", headers=headers, json=packet)).status_code == 410
        assert len(calls) == 1
    assert server.token not in json.dumps(calls)


def test_ephemeral_redaction_is_scoped_without_modifying_parent_environment():
    import os

    from firefly_forge.secrets import contains_known_credential, ephemeral_credentials, redact_environment

    token = "fixture-ephemeral-token-cc3cb2ef"
    assert "FORGE_CODING_RUNTIME_TOKEN" not in os.environ
    with ephemeral_credentials({"FORGE_CODING_RUNTIME_TOKEN": token}):
        assert redact_environment("message=" + token) == "message=[redacted]"
        assert contains_known_credential({"summary": token})
    assert redact_environment(token) == token
    assert "FORGE_CODING_RUNTIME_TOKEN" not in os.environ


@pytest.mark.asyncio
async def test_ephemeral_tokens_are_isolated_between_concurrent_invocations():
    import asyncio

    from firefly_forge.secrets import ephemeral_credentials, redact_environment

    async def check(own, other):
        with ephemeral_credentials({"FORGE_CODING_RUNTIME_TOKEN": own}):
            await asyncio.sleep(0)
            assert redact_environment(own + " " + other) == "[redacted] " + other

    await asyncio.gather(check("fixture-A-3e62", "fixture-B-985b"), check("fixture-B-985b", "fixture-A-3e62"))


@pytest.mark.asyncio
async def test_actual_source_snapshot_holds_an_ephemeral_token_written_into_project_source(tmp_path):
    from types import SimpleNamespace

    from test_execution import seed_repository

    from firefly_forge.engine import Engine
    from firefly_forge.process import checked
    from firefly_forge.secrets import ephemeral_credentials

    workspace = await seed_repository(tmp_path / "public-replica")
    base = await checked(["git", "rev-parse", "HEAD"], workspace)
    engine = Engine(SimpleNamespace(path=tmp_path / "fixture.db"), tmp_path / "workspaces")
    token = "fixture-MCP-source-token-d796c48b"
    (workspace / "implementation.py").write_text("value = " + repr(token) + "\n")
    with (
        ephemeral_credentials({"FORGE_CODING_RUNTIME_TOKEN": token}),
        pytest.raises(ValueError, match="configured credential"),
    ):
        await engine._snapshot(workspace, base)
    assert await checked(["git", "rev-parse", "HEAD"], workspace) == base


def test_coding_channel_deadline_never_outlives_selected_provider_timeout():
    from firefly_forge.coding_check_transport import CodingCheckServer

    async def unused(request):
        raise AssertionError("Deadline projection performs no call")

    now = int(time.time() * 1000)
    server = CodingCheckServer(unused, expires_at_ms=now + 3600000, invocation_timeout=2)
    assert now <= server.expires_at_ms <= now + 2100
    for invalid in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            CodingCheckServer(unused, expires_at_ms=now + 3600000, invocation_timeout=invalid)


@pytest.mark.asyncio
async def test_coding_channel_cancellation_drains_accepted_callback_before_transport_closes():
    import asyncio

    from firefly_forge.coding_check_transport import CodingCheckServer

    ready, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    finished = []

    async def callback(request):
        entered.set()
        await release.wait()
        finished.append(True)
        return {"exit_code": 0}

    server = CodingCheckServer(callback, expires_at_ms=int(time.time() * 1000) + 60000)

    async def own():
        async with server.running():
            ready.set()
            await asyncio.Event().wait()

    owner = asyncio.create_task(own())
    request = None
    try:
        await asyncio.wait_for(ready.wait(), 5)
        async with httpx.AsyncClient(timeout=5) as client:
            request = asyncio.create_task(
                client.post(
                    server.endpoint,
                    headers={
                        "authorization": "Bearer " + server.token,
                        "accept": "application/json, text/event-stream",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "run_configured_check",
                            "arguments": {"command_index": 0, "request_id": "a" * 32},
                        },
                    },
                )
            )
            await asyncio.wait_for(entered.wait(), 3)
            owner.cancel()
            await asyncio.sleep(0)
            owner.cancel()
            await asyncio.sleep(0)
            assert not owner.done() and finished == []
            release.set()
            assert (await request).status_code == 200
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(owner, 5)
        assert finished == [True] and server.active == set() and not server.accepting
    finally:
        release.set()
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
