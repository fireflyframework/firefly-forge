# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio
import contextlib
import json

from click.testing import CliRunner
from starlette.testclient import TestClient

from firefly_forge import cli
from firefly_forge.api import create_application
from firefly_forge.client import ForgeClient
from firefly_forge.engine import Engine
from firefly_forge.mcp_server import create_server


def test_native_tool_policy_sdk_cli_mcp_use_same_private_administration(tmp_path, postgres_schema, monkeypatch):
    async def idle(_self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Engine, "_poll", idle)
    token = "fixture-tool-policy-owner-token-32chars"

    class InProcess(TestClient):
        def request(self, *args, **kwargs):
            kwargs.pop("timeout", None)
            return super().request(*args, **kwargs)

    with InProcess(
        create_application(tmp_path, token),
        base_url="http://testserver/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 50000),
    ) as http:
        connection = ForgeClient("http://testserver", token)
        connection.http.close()
        connection.http = http
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(connection))
        assert http.get("tools/policy", headers={"Authorization": "Bearer invalid"}).status_code == 401
        assert connection.configure_tool_policy({"calls_limit": 2, "per_tool_limit": 1})["settings"]["calls_limit"] == 2
        view = CliRunner().invoke(cli.main, ["tools", "policy"])
        assert view.exit_code == 0 and json.loads(view.output) == connection.tool_policy()
        policy_file = tmp_path / "policy.json"
        policy_file.write_text('{"calls_limit": 4, "per_tool_limit": 2}')
        saved = CliRunner().invoke(cli.main, ["tools", "configure-policy", "--file", str(policy_file)])
        assert saved.exit_code == 0, saved.output
        assert connection.tool_policy()["settings"]["calls_limit"] == 4
        server = create_server()
        tools = {item.name: item for item in asyncio.run(server.list_tools())}
        assert "calls_limit" in str(tools["configure_tool_policy"].input_schema)
        result = asyncio.run(server.call_tool("configure_tool_policy", {"request": {"calls_limit": 3}}))
        assert not result.is_error and connection.tool_policy()["settings"]["calls_limit"] == 3
        schema = http.get("openapi.json").json()
        assert "/api/v1/runs/{id}/tool-invocations" in schema["paths"]
        assert "/api/v1/runs/{id}/tool-invocations/{correlation}/reconcile" in schema["paths"]
