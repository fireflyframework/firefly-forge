# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The supported local policy is identical through API, SDK, CLI and MCP."""

import asyncio
import contextlib
import json
from uuid import uuid4

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from firefly_forge import cli
from firefly_forge.api import create_application
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server


@pytest.mark.parametrize("origin", ["http://localhost:49152", "https://vendor.example", "http://127.0.0.1:49152/path"])
def test_sdk_rejects_unadmitted_provider_origin_before_http(origin):
    with ForgeClient("https://forge.example", "owned-operator-token-32-characters") as connection:
        calls = []
        connection.request = lambda *args, **kwargs: calls.append(args)
        with pytest.raises(ValueError):
            connection.configure_local_oauth_provider_policy(
                {"enabled": True, "local_origin": origin, "expected_revision": 0, "request_id": uuid4().hex}
            )
        assert calls == []


def test_supported_policy_api_sdk_cli_mcp_native_parity(tmp_path, postgres_schema, monkeypatch):
    monkeypatch.setenv("FORGE_OAUTH_LOCAL_DEVELOPMENT", "1")
    monkeypatch.setenv("FORGE_OIDC_ALLOW_LOOPBACK_HTTP", "true")
    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", "http://127.0.0.1:49200")
    token = "owned-policy-parity-operator-token-32-characters"

    class InProcess(TestClient):
        def request(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            return super().request(*args, **kwargs)

    with InProcess(
        create_application(tmp_path, token, role="api"),
        base_url="http://127.0.0.1:49200/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 50000),
    ) as http:
        connection = ForgeClient("http://127.0.0.1:49200", token)
        connection.http.close()
        connection.http = http
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        assert connection.oauth_provider_policies()["policies"] == []
        request_id = uuid4().hex
        configured = CliRunner().invoke(
            cli.main,
            [
                "connections",
                "oauth",
                "local-policy",
                "--origin",
                "http://[::1]:49152",
                "--enable",
                "--expected-revision",
                "0",
                "--request-id",
                request_id,
            ],
        )
        assert configured.exit_code == 0, configured.output
        packet = json.loads(configured.output)
        assert packet["revision"] == 1 and packet["provider"]["scopes"] == ["repository:read"]
        listed = CliRunner().invoke(cli.main, ["connections", "oauth", "policy-list"])
        assert listed.exit_code == 0 and json.loads(listed.output)["policies"] == [packet]
        server = create_server()
        tools = {item.name: item for item in asyncio.run(server.list_tools())}
        assert set(tools["configure_local_oauth_provider_policy"].input_schema["properties"]) == {
            "enabled",
            "local_origin",
            "expected_revision",
            "request_id",
        }
        result = asyncio.run(server.call_tool("get_oauth_provider_policies", {}))
        assert not result.is_error
        disabled = asyncio.run(
            server.call_tool(
                "configure_local_oauth_provider_policy",
                {
                    "enabled": False,
                    "local_origin": "http://[::1]:49152",
                    "expected_revision": 1,
                    "request_id": uuid4().hex,
                },
            )
        )
        assert not disabled.is_error
        assert connection.oauth_provider_policies()["policies"][0]["enabled"] is False
        stale = http.put(
            "oauth-provider-policies/local-repository-tools",
            json={
                "enabled": True,
                "local_origin": "http://[::1]:49152",
                "expected_revision": 0,
                "request_id": uuid4().hex,
            },
        )
        assert stale.status_code == 409
