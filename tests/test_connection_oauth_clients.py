# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio
import contextlib
import json

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient
from test_oauth_provider_policy_api import BASE, PROVIDER, registration

from firefly_forge import cli
from firefly_forge.api import create_application
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server


def test_native_oauth_metadata_configuration_handoff_sdk_cli_mcp_parity(tmp_path, postgres_schema, monkeypatch):
    token = "fixture-oauth-client-owner-token-32chars"
    monkeypatch.setenv("FORGE_OAUTH_LOCAL_DEVELOPMENT", "1")
    monkeypatch.setenv("FORGE_OIDC_ALLOW_LOOPBACK_HTTP", "true")
    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", "https://127.0.0.1:49200")
    monkeypatch.setenv("FORGE_ALLOWED_HOSTS", "127.0.0.1")

    class InProcess(TestClient):
        def request(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            return super().request(*args, **kwargs)

    with InProcess(
        create_application(tmp_path, token),
        base_url="https://127.0.0.1:49200/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 50000),
    ) as http:
        connection = ForgeClient("https://127.0.0.1:49200", token)
        connection.http.close()
        connection.http = http
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        assert http.put(BASE.removeprefix("/api/v1/") + "/" + PROVIDER, json=registration()).status_code == 200
        assert len(connection.connection_oauth_providers()) == 8
        key = tmp_path / "client.secret"
        key.write_text("fixture-private-client-secret")
        key.chmod(0o600)
        configured = CliRunner().invoke(
            cli.main,
            [
                "connections",
                "oauth",
                "configure",
                "design",
                "--provider",
                PROVIDER,
                "--client-id",
                "client",
                "--client-secret-file",
                str(key),
            ],
        )
        assert configured.exit_code == 0, configured.output
        assert "fixture-private-client-secret" not in configured.output
        roles = http.get("agent-roles").json()["document"]
        roles["roles"][0]["instructions"] = "Accidentally pasted fixture-private-client-secret"
        assert http.put("agent-roles", json=roles).status_code == 409
        assert connection.connection_oauth("design")["revision"] == 1
        prepared = CliRunner().invoke(cli.main, ["connections", "oauth", "prepare", "design"])
        assert prepared.exit_code == 0, prepared.output
        assert json.loads(prepared.output)["continuation_url"].startswith("https://127.0.0.1:49200/api/")
        assert "authorization_url" not in json.loads(prepared.output)
        server = create_server()
        tools = {item.name: item for item in asyncio.run(server.list_tools())}
        assert "client_secret" not in tools["configure_connection_oauth"].input_schema["properties"]
        assert "client_secret_file" in tools["configure_connection_oauth"].input_schema["properties"]
        assert "complete_connection_oauth" not in tools
        result = asyncio.run(server.call_tool("prepare_connection_oauth", {"name": "design"}))
        assert not result.is_error and "continuation_url" in str(result)
        revoked = asyncio.run(server.call_tool("disconnect_connection_oauth", {"name": "design"}))
        assert not revoked.is_error and connection.connection_oauth("design")["status"] == "disconnected"
        assert "fixture-private-client-secret" not in str(revoked)
        schema = http.get("openapi.json").json()
        assert schema["components"]["schemas"]["OAuthConnectionInput"]["properties"]["client_secret"]["anyOf"][0][
            "writeOnly"
        ]


def test_oauth_sdk_rejects_path_injection_before_http():
    connection = ForgeClient("https://forge.example", "fixture-token")
    try:
        with pytest.raises(ValueError):
            connection.prepare_connection_oauth("../../auth/callback")
    finally:
        connection.http.close()


@pytest.mark.parametrize("action", ["show", "configure", "prepare", "refresh", "disconnect"])
def test_oauth_sdk_rejects_reserved_catalog_name_before_http(action, monkeypatch):
    connection = ForgeClient("https://forge.example", "fixture-token")
    calls = []

    def unexpected_request(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Reserved names cannot send a request")

    monkeypatch.setattr(connection, "request", unexpected_request)
    methods = {
        "show": lambda: connection.connection_oauth("providers"),
        "configure": lambda: connection.configure_connection_oauth(
            "providers", {"provider": "fixture", "client_id": "client", "public_client": True}
        ),
        "prepare": lambda: connection.prepare_connection_oauth("providers"),
        "refresh": lambda: connection.refresh_connection_oauth("providers", "a" * 32),
        "disconnect": lambda: connection.disconnect_connection_oauth("providers"),
    }
    try:
        with pytest.raises(ValueError, match="name"):
            methods[action]()
        assert not calls
    finally:
        connection.http.close()
