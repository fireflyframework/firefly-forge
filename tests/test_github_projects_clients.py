# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio
import contextlib
import json

import httpx
import pytest
from click.testing import CliRunner

from firefly_forge import cli
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server


def test_sdk_paths_and_cli_use_same_typed_connection_contract(monkeypatch):
    calls = []
    view = {
        "version": 1,
        "project_id": "1" * 32,
        "repository_id": "2" * 32,
        "binding": None,
        "permissions": {"configure": True, "sync": True},
        "status": "not_configured",
        "latest_request": None,
    }

    def reply(request):
        calls.append(request)
        return httpx.Response(200, json=view)

    with ForgeClient("https://forge.example", "fixture-client-token") as client:
        client.http.close()
        client.http = httpx.Client(base_url="https://forge.example/api/v1/", transport=httpx.MockTransport(reply))
        result = client.github_projects_connection("1" * 32, "2" * 32)
        assert result.status == "not_configured"
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        output = CliRunner().invoke(cli.main, ["project", "github-projects", "show", "1" * 32, "2" * 32])
        assert output.exit_code == 0, output.output
        assert json.loads(output.output) == result.model_dump(mode="json")
        assert len(calls) == 2 and all(str(request.url).endswith("/github-projects/connection") for request in calls)


def test_sdk_rejects_paths_and_secret_bearing_requests_before_http(monkeypatch):
    with ForgeClient("https://forge.example", "fixture-client-token") as client:
        calls = []
        monkeypatch.setattr(client, "request", lambda *args, **kwargs: calls.append(args))
        with pytest.raises(ValueError):
            client.github_projects_connection("../secret", "2" * 32)
        with pytest.raises(ValueError):
            client.sync_github_projects(
                "1" * 32,
                "2" * 32,
                "3" * 32,
                {"version": 1, "request_id": "4" * 32, "expected_binding_digest": "a" * 64, "token": "secret"},
            )
        assert calls == []


def test_mcp_board_tools_are_typed_and_observation_cannot_replay():
    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}
    expected = {
        "get_github_projects_connection",
        "discover_github_projects",
        "configure_github_projects",
        "sync_github_projects",
        "get_github_projects_request",
        "observe_github_projects",
        "create_github_projects",
        "get_github_projects_creation",
    }
    assert expected <= tools.keys()
    assert tools["get_github_projects_connection"].annotations.read_only_hint
    assert not tools["observe_github_projects"].annotations.read_only_hint
    assert not tools["create_github_projects"].annotations.read_only_hint
    assert tools["get_github_projects_creation"].annotations.read_only_hint
    schema = tools["sync_github_projects"].input_schema
    assert "expected_binding_digest" in json.dumps(schema) and "access_token" not in json.dumps(schema)


def test_private_creation_uses_one_typed_post_and_same_scope_readback(monkeypatch):
    calls = []
    receipt = {
        "version": 1,
        "id": "3" * 32,
        "project_id": "1" * 32,
        "repository_id": "2" * 32,
        "request_digest": "a" * 64,
        "status": "unknown",
        "board": None,
        "replay_authorized": False,
        "created_at": "2026-10-31T00:00:00Z",
    }
    with ForgeClient("https://forge.example", "synthetic-token") as connection:

        def reply(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return receipt

        monkeypatch.setattr(connection, "request", reply)
        body = {
            "version": 1,
            "request_id": "3" * 32,
            "connection_name": "github-board",
            "owner": "octocat",
            "title": "Delivery",
            "acknowledge_private_creation": True,
        }
        result = connection.create_github_projects("1" * 32, "2" * 32, body)
        assert result.status == "unknown"
        assert connection.github_projects_creation("1" * 32, "2" * 32, "3" * 32) == result
        assert [call[0] for call in calls] == ["POST", "GET"]
        assert calls[0][1].endswith("/github-projects/creations")
        assert calls[1][1].endswith("/github-projects/creations/" + "3" * 32)


def test_sdk_rejects_well_formed_foreign_board_response(monkeypatch):
    with ForgeClient("https://forge.example", "synthetic-token") as connection:
        monkeypatch.setattr(
            connection,
            "request",
            lambda *args, **kwargs: {
                "version": 1,
                "project_id": "9" * 32,
                "repository_id": "2" * 32,
                "binding": None,
                "permissions": {"configure": False, "sync": False},
                "status": "not_configured",
                "latest_request": None,
            },
        )
        with pytest.raises(ValueError, match="scope"):
            connection.github_projects_connection("1" * 32, "2" * 32)
