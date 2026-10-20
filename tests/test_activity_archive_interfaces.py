# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native HTTP, SDK, CLI, and MCP use the same exact read contract."""

import importlib
import json
from contextlib import nullcontext
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner
from test_activity_archive import EVENT, RUN

from firefly_forge.activity_models import ActivityUnavailable
from firefly_forge.client import ForgeClient


@pytest.mark.parametrize("unavailable", [False, True])
async def test_controller_returns_bounded_content_or_explicit_unavailable(unavailable):
    module = importlib.import_module("firefly_forge.activity_controllers")
    calls = []

    async def content(run_id, event_id, query):
        calls.append((run_id, event_id, query.model_dump()))
        if unavailable:
            raise ActivityUnavailable("activity_cut_changed")
        return {"version": 1, "data_base64": "cHVibGlj", "next_chunk": None}

    controller = module.ActivityContentController(SimpleNamespace(content=content))
    response = await controller.content(RUN, EVENT, expected_descriptor="d" * 64, chunk=0)
    assert response.status_code == (409 if unavailable else 200)
    assert "no-store" in response.headers["Cache-Control"]
    body = json.loads(response.body)
    assert body == (
        {"state": "unavailable", "reason": "activity_cut_changed"}
        if unavailable
        else {"version": 1, "data_base64": "cHVibGlj", "next_chunk": None}
    )
    assert calls == [(RUN, EVENT, {"expected_descriptor": "d" * 64, "chunk": 0})]


def test_sdk_validates_the_exact_descriptor_and_chunk_before_reading():
    calls = []

    def transport(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path == f"/api/v1/runs/{RUN}/activity/{EVENT}/content"
        assert dict(request.url.params) == {"expected_descriptor": "d" * 64, "chunk": "1"}
        return httpx.Response(200, json={"version": 1, "data_base64": "cHVibGlj", "next_chunk": None})

    connection = ForgeClient("https://factory.invalid", "public-fixture-token")
    connection.http.close()
    connection.http = httpx.Client(base_url="https://factory.invalid/api/v1/", transport=httpx.MockTransport(transport))
    try:
        result = connection.run_activity_content(RUN, EVENT, {"expected_descriptor": "d" * 64, "chunk": 1})
        assert result["data_base64"] == "cHVibGlj" and len(calls) == 1
        with pytest.raises(ValueError):
            connection.run_activity_content(RUN, EVENT, {"expected_descriptor": "d" * 64, "chunk": True})
        assert len(calls) == 1
    finally:
        connection.http.close()


def test_cli_activity_read_requires_exact_identity_and_preserves_chunk_bounds(monkeypatch):
    from firefly_forge import cli

    calls = []

    class Client:
        def run_activity_content(self, run_id, event_id, query):
            calls.append((run_id, event_id, query))
            return {"version": 1, "data_base64": "cHVibGlj", "next_chunk": None}

    monkeypatch.setattr(cli, "client", lambda: nullcontext(Client()))
    runner = CliRunner()
    result = runner.invoke(
        cli.main, ["evidence", "activity", RUN, EVENT, "--expected-descriptor", "d" * 64, "--chunk", "1"]
    )
    assert result.exit_code == 0 and "cHVibGlj" in result.output
    assert calls == [(RUN, EVENT, {"expected_descriptor": "d" * 64, "chunk": 1})]
    refused = runner.invoke(
        cli.main, ["evidence", "activity", RUN, EVENT, "--expected-descriptor", "d" * 64, "--chunk", "256"]
    )
    assert refused.exit_code != 0 and len(calls) == 1


async def test_mcp_activity_tool_is_native_read_only_and_bounded():
    from firefly_forge.mcp_server import create_server

    tools = await create_server().list_tools()
    tool = next(tool for tool in tools if tool.name == "get_run_activity_content")
    annotations = tool.annotations.model_dump(by_alias=True)
    assert annotations["readOnlyHint"] is True and annotations["openWorldHint"] is False
    query = tool.input_schema["properties"]["query"]
    assert query["properties"]["chunk"]["maximum"] == 255
    assert "expected_descriptor" in query["required"]
