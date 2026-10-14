# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native HTTP directory privacy and SDK/CLI/MCP recovery parity."""

import asyncio
import contextlib
import json

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient
from test_connection_oauth_directory import effect
from test_connection_oauth_ledger import provider

from firefly_forge import cli
from firefly_forge.api import create_application
from firefly_forge.client import ForgeClient
from firefly_forge.connection_oauth import ConnectionOAuth
from firefly_forge.mcp_server import create_server


def test_native_directory_effect_supersession_http_sdk_cli_mcp_contract(tmp_path, postgres_schema, monkeypatch):
    token = "owned-directory-operator-token-32-characters"
    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", "https://forge.example")
    monkeypatch.setenv("FORGE_ALLOWED_HOSTS", "forge.example")

    class InProcess(TestClient):
        def request(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            return super().request(*args, **kwargs)

    with InProcess(
        create_application(tmp_path, token),
        base_url="https://forge.example/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 50000),
    ) as http:
        connection = ForgeClient("https://forge.example", token)
        connection.http.close()
        connection.http = http
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        vault = http.app.state.runtime.context.get_bean(ConnectionOAuth)
        vault.catalog["fixture"] = provider()
        connection.configure_connection_oauth(
            "design", {"provider": "fixture", "client_id": "client", "public_client": True}
        )
        directory = http.get("connection-oauth")
        assert directory.status_code == 200, directory.text
        assert directory.headers["cache-control"] == "no-store"
        assert directory.headers["referrer-policy"] == "no-referrer"
        listed = CliRunner().invoke(cli.main, ["connections", "oauth", "list"])
        assert listed.exit_code == 0, listed.output
        assert json.loads(listed.output)[0]["name"] == "design"
        unknown = http.portal.call(effect, vault.store)

        async def request_id():
            from uuid import UUID

            from firefly_forge.connection_oauth_persistence import OAuthEffectEntity

            async with vault.store._unit() as session:
                return (await session.get(OAuthEffectEntity, UUID(unknown))).request_id

        request = http.portal.call(request_id)
        observed = CliRunner().invoke(cli.main, ["connections", "oauth", "effect", "design", "--request-id", request])
        assert observed.exit_code == 0, observed.output
        assert json.loads(observed.output)["state"] == "unknown"
        server = create_server()
        tools = {entry.name: entry for entry in asyncio.run(server.list_tools())}
        assert "client_secret" not in tools["supersede_connection_oauth"].input_schema["properties"]
        assert "client_secret_file" in tools["supersede_connection_oauth"].input_schema["properties"]
        assert not asyncio.run(server.call_tool("list_connection_oauth", {})).is_error
        assert not asyncio.run(
            server.call_tool("get_connection_oauth_effect", {"name": "design", "request_id": request})
        ).is_error
        superseded = asyncio.run(
            server.call_tool(
                "supersede_connection_oauth",
                {
                    "name": "design",
                    "expected_revision": 1,
                    "unknown_effect_ids": [unknown],
                    "reason": "Retain the interrupted outcome while admitting a new authorization.",
                    "acknowledge_unknown": True,
                    "provider": "fixture",
                    "client_id": "client",
                    "public_client": True,
                },
            )
        )
        assert not superseded.is_error, str(superseded)
        summary = connection.connection_oauth("design")
        assert summary["revision"] == 2 and summary["archived_unknown_effects"][0]["id"] == unknown
        assert summary["allowed_actions"]["authorize"]
        next_unknown = http.portal.call(effect, vault.store)
        cli_superseded = CliRunner().invoke(
            cli.main,
            [
                "connections",
                "oauth",
                "supersede",
                "design",
                "--expected-revision",
                "2",
                "--unknown-effect-id",
                next_unknown,
                "--reason",
                "Preserve the second interrupted outcome and start a new admission.",
                "--acknowledge-unknown",
                "--provider",
                "fixture",
                "--client-id",
                "client",
                "--public-client",
            ],
        )
        assert cli_superseded.exit_code == 0, cli_superseded.output
        assert json.loads(cli_superseded.output)["revision"] == 3
        schema = http.get("openapi.json").json()
        assert "/api/v1/connection-oauth/{name}/effects/{request_id}" in schema["paths"]
        assert "OAuthConnectionSummary" in schema["components"]["schemas"]


@pytest.mark.parametrize("action", ["directory", "effect", "supersede"])
def test_invalid_scopes_acknowledgment_or_path_never_dispatches_http(action, monkeypatch):
    connection = ForgeClient("https://forge.example", "owned-token")
    calls = []
    monkeypatch.setattr(connection, "request", lambda *args, **kwargs: calls.append((args, kwargs)))
    try:
        with pytest.raises(ValueError):
            if action == "directory":
                connection.connection_oauth_connections("../private")
            elif action == "effect":
                connection.connection_oauth_effect("design", "../private")
            else:
                connection.supersede_connection_oauth(
                    "design",
                    {
                        "expected_revision": 1,
                        "unknown_effect_ids": ["a" * 32],
                        "reason": "A new admission is required.",
                        "acknowledge_unknown": False,
                        "registration": {"provider": "fixture", "client_id": "client", "public_client": True},
                    },
                )
        assert calls == []
    finally:
        connection.http.close()
