# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import httpx
from click.testing import CliRunner

from firefly_forge.cli import main
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server
from firefly_forge.models import RunInput


def test_client_carries_authentication_and_validated_run():
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fixture-token"
        assert request.url.path == "/api/v1/runs"
        assert b'"publish":false' in request.content
        return httpx.Response(201, json={"id": "run-one", "status": "queued"})

    with ForgeClient("http://factory.invalid", "fixture-token") as client:
        client.http.close()
        client.http = httpx.Client(
            base_url="http://factory.invalid/api/v1/",
            headers={"Authorization": "Bearer fixture-token"},
            transport=httpx.MockTransport(handle),
        )
        result = client.create_run(RunInput(repository_id="repo-one", brief="Create real product"))
        assert result["id"] == "run-one"


async def test_mcp_tools_have_real_input_contracts():
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    assert set(tools) == {
        "list_repositories",
        "connect_repository",
        "create_run",
        "get_run",
        "cancel_run",
        "publish_run",
    }
    assert tools["create_run"].input_schema["required"] == ["repository_id", "brief"]
    assert "publish" not in tools["create_run"].input_schema["properties"]


def test_cli_version_and_command_help():
    runner = CliRunner()
    assert "0.1.0" in runner.invoke(main, ["--version"]).output
    result = runner.invoke(main, ["run", "--help"])
    assert result.exit_code == 0
    assert "--check" in result.output and "--publish" in result.output
