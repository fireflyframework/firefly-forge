# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Operator adapters retain the explicit request and never retry uncertain delivery."""

import json
from contextlib import contextmanager

import httpx
import pytest
from click.testing import CliRunner

from firefly_forge.cli import main
from firefly_forge.client import ForgeClient
from firefly_forge.mcp_server import create_server

RUN, REQUEST, EXECUTION = "a" * 32, "b" * 32, "c" * 32
BODY = {"request_id": REQUEST, "execution_id": EXECUTION}
RECEIPT = {"id": REQUEST, "status": "completed", "result": {"disposition": "accepted"}}


@contextmanager
def connection(handler):
    with ForgeClient("http://factory.invalid", "adapter-fixture-token") as client:
        client.http.close()
        client.http = httpx.Client(
            base_url="http://factory.invalid/api/v1/",
            headers={"Authorization": "Bearer adapter-fixture-token"},
            transport=httpx.MockTransport(handler),
        )
        yield client


def test_sdk_retains_exact_request_body_and_reads_same_operation_without_replay():
    seen = []

    def handle(request):
        assert request.headers["Authorization"] == "Bearer adapter-fixture-token"
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        return httpx.Response(200, json=RECEIPT)

    with connection(handle) as client:
        assert client.request_runner_continuation(RUN, BODY) == RECEIPT
        assert client.runner_continuation(RUN, REQUEST) == RECEIPT
    assert seen == [
        ("POST", f"/api/v1/runs/{RUN}/runner-continuations", BODY),
        ("GET", f"/api/v1/runs/{RUN}/runner-continuations/{REQUEST}", None),
    ]


@pytest.mark.parametrize("status", [403, 404, 409, 422, 500])
def test_sdk_error_does_not_retry_or_allocate_another_identity(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"detail": "Retain the original operation"})

    with connection(handle) as client, pytest.raises(RuntimeError, match=f"HTTP {status}"):
        client.request_runner_continuation(RUN, BODY)
    assert len(calls) == 1
    assert json.loads(calls[0].content) == BODY


def test_sdk_response_loss_requires_explicit_exact_readback():
    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("Lost response", request=request)
        return httpx.Response(200, json=RECEIPT)

    with connection(handle) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.request_runner_continuation(RUN, BODY)
        assert len(calls) == 1
        assert client.runner_continuation(RUN, REQUEST) == RECEIPT
    assert [request.method for request in calls] == ["POST", "GET"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"request_id": REQUEST},
        {**BODY, "execution_id": True},
        {**BODY, "request_id": "secret-marker-invalid"},
        {**BODY, "execution_id": "../secret-marker-invalid"},
        {**BODY, "actor": "secret-marker-invalid"},
        {**BODY, "force": True},
    ],
)
def test_sdk_rejects_invalid_admission_before_outbound_io_without_reflecting_input(body):
    calls = []
    with (
        connection(lambda request: calls.append(request) or httpx.Response(200, json={})) as client,
        pytest.raises(ValueError) as error,
    ):
        client.request_runner_continuation(RUN, body)
    assert "secret-marker-invalid" not in str(error.value)
    assert calls == []


@pytest.mark.parametrize(
    "method,args", [("request_runner_continuation", ("../run", BODY)), ("runner_continuation", (RUN, "../receipt"))]
)
def test_sdk_rejects_path_injection_without_io(method, args):
    calls = []
    with (
        connection(lambda request: calls.append(request) or httpx.Response(200, json={})) as client,
        pytest.raises(ValueError),
    ):
        getattr(client, method)(*args)
    assert calls == []


def test_cli_requires_explicit_ids_and_reads_the_same_receipt(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=RECEIPT)

    monkeypatch.setattr("firefly_forge.cli.client", lambda: connection(handle))
    runner = CliRunner()
    missing = runner.invoke(main, ["request-runner-continuation", RUN, "--execution-id", EXECUTION])
    assert missing.exit_code == 2 and "--request-id" in missing.output
    assert calls == []
    result = runner.invoke(
        main, ["request-runner-continuation", RUN, "--request-id", REQUEST, "--execution-id", EXECUTION]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == RECEIPT
    read = runner.invoke(main, ["runner-continuation", RUN, REQUEST])
    assert read.exit_code == 0, read.output
    assert json.loads(read.output) == RECEIPT
    assert [request.method for request in calls] == ["POST", "GET"]
    assert json.loads(calls[0].content) == BODY


async def test_mcp_declares_strict_admission_and_read_only_exact_receipt(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=RECEIPT)

    monkeypatch.setattr("firefly_forge.cli.client", lambda: connection(handle))
    server = create_server()
    tools = {tool.name: tool for tool in await server.list_tools()}
    submit = tools["request_runner_continuation"]
    read = tools["get_runner_continuation"]
    assert submit.input_schema["required"] == ["identifier", "request"]
    contract = submit.input_schema["properties"]["request"]
    assert contract["additionalProperties"] is False
    assert contract["required"] == ["request_id", "execution_id"]
    assert contract["properties"]["request_id"]["pattern"] == r"^[a-f0-9]{32}$"
    assert submit.annotations.read_only_hint is False
    assert submit.annotations.idempotent_hint is False
    assert read.annotations.read_only_hint is True
    assert read.annotations.idempotent_hint is True
    assert read.input_schema["required"] == ["identifier", "request_id"]
    result = await server.call_tool("request_runner_continuation", {"identifier": RUN, "request": BODY})
    assert "accepted" in str(result)
    result = await server.call_tool("get_runner_continuation", {"identifier": RUN, "request_id": REQUEST})
    assert "accepted" in str(result)
    assert [request.method for request in calls] == ["POST", "GET"]
    assert json.loads(calls[0].content) == BODY


async def test_mcp_response_loss_never_retries_and_exact_read_is_a_separate_call(monkeypatch):
    from mcp.server.mcpserver.exceptions import UnexpectedToolError

    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("Retained identity must be read", request=request)
        return httpx.Response(200, json=RECEIPT)

    monkeypatch.setattr("firefly_forge.cli.client", lambda: connection(handle))
    server = create_server()
    with pytest.raises(UnexpectedToolError):
        await server.call_tool("request_runner_continuation", {"identifier": RUN, "request": BODY})
    assert len(calls) == 1
    await server.call_tool("get_runner_continuation", {"identifier": RUN, "request_id": REQUEST})
    assert [request.method for request in calls] == ["POST", "GET"]


async def test_mcp_rejects_forged_admission_fields_without_any_outbound_call(monkeypatch):
    from mcp.server.mcpserver.exceptions import UnexpectedToolError

    calls = []
    monkeypatch.setattr(
        "firefly_forge.cli.client",
        lambda: connection(lambda request: calls.append(request) or httpx.Response(200, json={})),
    )
    with pytest.raises(UnexpectedToolError):
        await create_server().call_tool(
            "request_runner_continuation", {"identifier": RUN, "request": {**BODY, "force": True}}
        )
    assert calls == []
