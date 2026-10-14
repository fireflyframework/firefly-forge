# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual native grants supply MCP authorization without private token files."""

import pytest
from test_connection_oauth_flows import local_provider
from test_oauth_resource_custody import admitted


async def test_native_oauth_mcp_link_keeps_tokens_out_of_private_revision_files(store, tmp_path):
    from firefly_forge.mcp_registry import ExternalMCPRegistry, MCPOAuthConnectionInput

    async with local_provider() as (origin, *_):
        resources, _, user, binding, _ = await admitted(store, tmp_path, origin)
        registry = ExternalMCPRegistry(store.configuration, resources=resources)
        with pytest.raises(ValueError, match="policy|resource"):
            await registry.link_oauth(
                "local-repository-tools",
                MCPOAuthConnectionInput(connection_name="design", project_id=binding["project_id"]),
                user,
            )
        assert not list(registry.revisions.glob("*.json"))


@pytest.mark.parametrize(
    "body",
    [
        {"connection_name": "../admin", "project_id": "a" * 32},
        {"connection_name": "design", "project_id": "not-a-project"},
        {"connection_name": "design", "project_id": "a" * 32, "enabled": 1},
        {"connection_name": "design", "project_id": "a" * 32, "access_token": "never-admitted"},
    ],
)
def test_oauth_link_sdk_rejects_invalid_or_credential_inputs_before_http(body):
    from firefly_forge.client import ForgeClient

    with ForgeClient("https://forge.example", "owned-operator-token-32-characters") as client:
        calls = []
        client.request = lambda *args, **kwargs: calls.append((args, kwargs))
        with pytest.raises(ValueError):
            client.link_external_mcp_oauth("local-repository-tools", body)
        assert calls == []


def test_native_oauth_link_api_sdk_cli_mcp_share_the_write_only_free_contract(tmp_path, postgres_schema, monkeypatch):
    import asyncio
    import contextlib

    from click.testing import CliRunner
    from starlette.testclient import TestClient

    from firefly_forge import cli
    from firefly_forge.api import create_application
    from firefly_forge.client import ForgeClient
    from firefly_forge.mcp_server import create_server

    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", "https://forge.example")
    monkeypatch.setenv("FORGE_ALLOWED_HOSTS", "forge.example")
    token = "owned-mcp-oauth-operator-token-32-characters"

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
        source = http.get("openapi.json").json()
        assert "/api/v1/tools/mcp/{connector}/oauth" in source["paths"]
        fields = source["components"]["schemas"]["MCPOAuthConnectionInput"]["properties"]
        assert set(fields) == {"connection_name", "project_id", "enabled"}
        result = http.put(
            "tools/mcp/local-repository-tools/oauth", json={"connection_name": "missing", "project_id": "a" * 32}
        )
        assert result.status_code == 404
        connection = ForgeClient("https://forge.example", token)
        connection.http.close()
        connection.http = http
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        invoked = CliRunner().invoke(
            cli.main,
            ["tools", "mcp", "oauth", "local-repository-tools", "--connection", "missing", "--project-id", "a" * 32],
        )
        assert invoked.exit_code != 0
        server = create_server()
        tools = {item.name: item for item in asyncio.run(server.list_tools())}
        assert set(tools["link_external_mcp_oauth"].input_schema["properties"]) == {
            "connector",
            "connection_name",
            "project_id",
            "enabled",
        }
        from mcp.server.mcpserver.exceptions import UnexpectedToolError

        with pytest.raises(UnexpectedToolError, match="link_external_mcp_oauth"):
            asyncio.run(
                server.call_tool(
                    "link_external_mcp_oauth",
                    {"connector": "local-repository-tools", "connection_name": "missing", "project_id": "a" * 32},
                )
            )


async def test_public_mcp_catalog_checks_current_replica_credentials(tmp_path, postgres_schema, monkeypatch):
    from test_connection_oauth_ledger import actor, configuration
    from test_oauth_vault_admission import replicas

    async with replicas(tmp_path, monkeypatch) as (services, reader):
        await services[0].configure("design", configuration(client_secret="GitHub"), actor())
        response = await reader.get("/api/v1/tools/mcp")
        assert response.status_code == 409, response.text
        assert "GitHub" not in response.text
