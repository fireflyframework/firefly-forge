# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Candidate adapters inspect local evidence without admitting a continuation."""

import asyncio
import contextlib
import json

import httpx
import pytest
from click.testing import CliRunner
from mcp.server.mcpserver.exceptions import UnexpectedToolError

from firefly_forge import cli
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server

RUN = "a" * 32
TOOL = "get_runner_continuation_candidates"


def invoke(interface, client, identifier=RUN):
    if interface == "sdk":
        return client.runner_continuation_candidates(identifier)
    if interface == "cli":
        result = CliRunner().invoke(cli.main, ["runner-continuation-candidates", identifier])
        if result.exit_code:
            raise result.exception or AssertionError(result.output)
        return json.loads(result.output)
    result = asyncio.run(create_server().call_tool(TOOL, {"identifier": identifier}))
    assert not result.is_error, str(result)
    return result.structured_content


@contextlib.contextmanager
def connection(monkeypatch, handler):
    with ForgeClient("http://factory.invalid", "owned-adapter-token") as client:
        client.http.close()
        client.http = httpx.Client(
            base_url="http://factory.invalid/api/v1/",
            headers={"Authorization": "Bearer owned-adapter-token"},
            transport=httpx.MockTransport(handler),
        )
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        yield client


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
@pytest.mark.parametrize("state", ["requires_leader_confirmation", "source_unavailable", "no_candidate"])
def test_candidate_adapters_forward_one_authenticated_get_and_preserve_local_state(monkeypatch, interface, state):
    response = {
        "version": 1,
        "run_id": RUN,
        "state": state,
        "reason": "Local checks still require leader confirmation",
        "commands": [{"execution_id": "b" * 32, "command_label": "Python command", "argument_count": 2}]
        if state == "requires_leader_confirmation"
        else [],
    }
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path, request.content))
        assert request.headers["Authorization"] == "Bearer owned-adapter-token"
        assert not request.url.query
        return httpx.Response(200, json=response)

    with connection(monkeypatch, handle) as client:
        assert invoke(interface, client) == response
    assert calls == [("GET", f"/api/v1/runs/{RUN}/runner-continuations/candidates", b"")]


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
@pytest.mark.parametrize("status", [401, 403, 404, 409, 500, "lost"])
def test_candidate_errors_never_retry_or_submit(monkeypatch, interface, status):
    calls = []

    def handle(request):
        calls.append(request)
        if status == "lost":
            raise httpx.ReadTimeout("Owned lost read", request=request)
        return httpx.Response(status, json={"detail": "Owned refusal"})

    with (
        connection(monkeypatch, handle) as client,
        pytest.raises((RuntimeError, httpx.ReadTimeout, UnexpectedToolError)),
    ):
        invoke(interface, client)
    assert [(call.method, call.url.path) for call in calls] == [
        ("GET", f"/api/v1/runs/{RUN}/runner-continuations/candidates")
    ]


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_candidate_path_injection_is_rejected_before_io(monkeypatch, interface):
    calls = []
    with connection(monkeypatch, lambda request: calls.append(request) or httpx.Response(200, json={})) as client:
        with pytest.raises((ValueError, UnexpectedToolError)) as error:
            invoke(interface, client, "../owned-private-canary")
        assert "owned-private-canary" not in str(error.value)
    assert calls == []


async def test_candidate_tool_is_explicitly_read_only_and_requires_only_run_identity():
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    tool = tools[TOOL]
    assert len(tools) == 131
    assert tool.input_schema["required"] == ["identifier"]
    assert set(tool.input_schema["properties"]) == {"identifier"}
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is False
    assert tool.annotations.destructive_hint is False


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_native_candidate_clients_read_role_api_without_dispatch(postgres_schema, tmp_path, monkeypatch, interface):
    from starlette.testclient import TestClient
    from test_runner_continuation_candidates import command_fixture

    from firefly_forge.api import create_application
    from firefly_forge.project_access import CURRENT_PRINCIPAL
    from firefly_forge.runner_continuations import RunnerContinuations
    from firefly_forge.store import Store

    calls = []
    token = "owned-native-candidate-client-bootstrap"

    class NativeHTTP(TestClient):
        def request(self, method, url, *args, **kwargs):
            kwargs.pop("timeout", None)
            calls.append((method, str(url)))
            return super().request(method, url, *args, **kwargs)

    with NativeHTTP(
        create_application(tmp_path / "api", token, role="api"),
        base_url="http://testserver/api/v1/",
        headers={"Authorization": "Bearer " + token},
        client=("127.0.0.1", 52000),
    ) as http:
        context = http.app.state.runtime.context
        store = context.get_bean(Store)
        service = context.get_bean(RunnerContinuations)
        assert service.runtime.role == "api" and service.worker is None
        (store.path.parent / "workspaces").rmdir()  # Only the empty directory created by this owned API fixture.

        async def seed():
            fixture = await command_fixture(store, ["git", "diff"])
            CURRENT_PRINCIPAL.reset(fixture[6])
            return fixture

        fixture = http.portal.call(seed)
        manager, controller, run, journal, *_ = fixture
        try:
            before = http.portal.call(journal.entries, run["id"])
            executions = http.portal.call(journal.sandbox_executions, run["id"])
            with ForgeClient("http://testserver", token) as client:
                client.http.close()
                client.http = http
                monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
                result = invoke(interface, client, run["id"])
                assert result["state"] == "requires_leader_confirmation"
                command = result["commands"][0]
                assert command["execution_id"] == executions[0]["execution_id"]
                assert command["command_label"] == "Git command" and command["argument_count"] == 1
                assert "argv" not in command and "workspace" not in command
                path = f"runs/{run['id']}/runner-continuations/candidates"
                assert calls == [("GET", path)]
                assert http.get(path, headers={"Authorization": "Bearer invalid-owned-token"}).status_code == 401
                assert http.get(f"runs/{'e' * 32}/runner-continuations/candidates").status_code == 404
                assert http.portal.call(journal.entries, run["id"]) == before
                assert http.portal.call(journal.sandbox_executions, run["id"]) == executions
                assert http.portal.call(manager.requests.for_run, run["id"]) == []
                assert controller.submissions == 1 and controller.continuations == 0
        finally:

            async def drain():
                fixture[5].set()
                fixture[4].cancel()
                await asyncio.gather(fixture[4], return_exceptions=True)

            http.portal.call(drain)
