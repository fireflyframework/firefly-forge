# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Linked verification adapters retain explicit consent and one operation identity."""

import asyncio
import contextlib
import copy
import json

import httpx
import pytest
from click.testing import CliRunner
from linked_verification_fixtures import candidate, consent, receipt
from mcp.server.mcpserver.exceptions import ToolError
from test_original_selection_clients_host import connection

from firefly_forge import cli
from firefly_forge.execution_requests import digest as request_digest
from firefly_forge.journal import digest
from firefly_forge.linked_verification_models import LinkedVerificationInputV1
from firefly_forge.mcp_server import create_server

CANDIDATE = candidate(effect_budget=7)
BODY = consent(CANDIDATE)
RECEIPT = receipt(CANDIDATE)
RUN, REQUEST = CANDIDATE["run_id"], BODY["request_id"]
ACKNOWLEDGMENTS = [key for key in BODY if key.startswith("acknowledge_")]


def invoke(interface, operation, client, body=BODY, run_id=RUN, request_id=REQUEST):
    if interface == "sdk":
        if operation == "candidate":
            return client.linked_verification_candidate(run_id)
        if operation == "status":
            return client.linked_verification_status(run_id, request_id)
        return client.request_linked_verification(run_id, body)
    if interface == "cli":
        args = ["linked-verifications", operation, run_id]
        if operation == "status":
            args.append(request_id)
        if operation == "request":
            args += ["--request-id", body["request_id"], "--candidate-digest", body["candidate_digest"]]
            args += ["--" + key.replace("_", "-") for key in ACKNOWLEDGMENTS if body.get(key) is True]
        result = CliRunner().invoke(cli.main, args)
        if result.exit_code:
            raise result.exception or ValueError(result.output)
        return json.loads(result.output)
    tool = {
        "candidate": "get_linked_verification_candidate",
        "request": "request_linked_verification",
        "status": "get_linked_verification_status",
    }[operation]
    arguments = {"identifier": run_id}
    if operation == "status":
        arguments["request_id"] = request_id
    if operation == "request":
        arguments["request"] = body
    result = asyncio.run(create_server().call_tool(tool, arguments))
    if result.is_error:
        raise ValueError("Linked verification tool refused")
    return result.structured_content


def bind(monkeypatch, client):
    monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))


async def test_linked_verification_tools_require_native_consent_and_read_annotations():
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    for name, fields in (
        ("get_linked_verification_candidate", ["identifier"]),
        ("get_linked_verification_status", ["identifier", "request_id"]),
    ):
        tool = tools[name]
        assert tool.input_schema["required"] == fields
        assert set(tool.input_schema["properties"]) == set(fields)
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.idempotent_hint is True
        assert tool.annotations.open_world_hint is False
        for field in fields:
            assert tool.input_schema["properties"][field]["pattern"] == "^[a-f0-9]{32}$"
    tool = tools["request_linked_verification"]
    assert tool.input_schema["required"] == ["identifier", "request"]
    assert set(tool.input_schema["properties"]) == {"identifier", "request"}
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.idempotent_hint is True
    body = tool.input_schema["properties"]["request"]
    assert body["additionalProperties"] is False
    assert set(body["required"]) == {
        "version",
        "request_id",
        "candidate_digest",
        "acknowledge_all_fresh_checks",
        "acknowledge_no_coding_or_repair",
        "acknowledge_original_history_and_budget_preserved",
    }
    assert body["properties"]["version"]["const"] == 1
    assert body["properties"]["request_id"]["pattern"] == "^[a-f0-9]{32}$"
    assert body["properties"]["candidate_digest"]["pattern"] == "^[a-f0-9]{64}$"
    for field in body["required"]:
        if field.startswith("acknowledge_"):
            assert body["properties"][field]["const"] is True


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
@pytest.mark.parametrize("operation", ["candidate", "request", "status"])
def test_one_authenticated_request_uses_exact_native_shape(monkeypatch, interface, operation):
    calls = []
    response = CANDIDATE if operation == "candidate" else RECEIPT
    original = copy.deepcopy(BODY)
    with connection(lambda request: calls.append(request) or httpx.Response(200, json=response)) as client:
        bind(monkeypatch, client)
        assert invoke(interface, operation, client) == response
    assert original == BODY
    assert len(calls) == 1
    suffix = {"candidate": "/candidate", "request": "", "status": "/" + REQUEST}[operation]
    assert calls[0].url.path == f"/api/v1/runs/{RUN}/linked-verifications{suffix}"
    assert calls[0].headers["Authorization"] == "Bearer fictional-operator-token"
    assert not calls[0].url.query
    assert calls[0].method == ("POST" if operation == "request" else "GET")
    assert (json.loads(calls[0].content) if operation == "request" else calls[0].content) == (
        BODY if operation == "request" else b""
    )


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
@pytest.mark.parametrize("status", ["queued", "executing", "failed", "needs_reconciliation", "completed"])
def test_status_preserves_native_progress_and_only_actual_completed_settlement(monkeypatch, interface, status):
    response = receipt(CANDIDATE, status=status)
    if status == "needs_reconciliation":
        response["progress"].update(started=1, unknown=True)
    calls = []
    with connection(lambda request: calls.append(request) or httpx.Response(200, json=response)) as client:
        bind(monkeypatch, client)
        observed = invoke(interface, "status", client)
    assert observed == response
    assert ("settlement" in observed) == (status == "completed")
    assert observed["no_coding_grant"] is observed["original_budget_preserved"] is True
    assert len(calls) == 1 and calls[0].method == "GET"


@pytest.mark.parametrize("interface", ["sdk", "mcp"])
@pytest.mark.parametrize(
    "mutation",
    [
        {"version": True},
        {"request_id": "../private-request-canary"},
        {"candidate_digest": "A" * 64},
        {"acknowledge_all_fresh_checks": 1},
        {"acknowledge_no_coding_or_repair": False},
        {"acknowledge_original_history_and_budget_preserved": "true"},
        {"private-extra-canary": True},
    ],
)
def test_strict_native_consent_refuses_before_transport_without_echo(monkeypatch, interface, mutation):
    calls = []
    with connection(lambda request: calls.append(request) or httpx.Response(200, json=RECEIPT)) as client:
        bind(monkeypatch, client)
        with pytest.raises((ValueError, ToolError)) as failure:
            invoke(interface, "request", client, body=BODY | mutation)
    assert calls == []
    assert "private-request-canary" not in str(failure.value)
    assert "private-extra-canary" not in str(failure.value)


def test_constructed_native_input_does_not_bypass_validation():
    body = LinkedVerificationInputV1.model_construct(**(BODY | {"acknowledge_all_fresh_checks": 1}))
    calls = []
    with (
        connection(lambda request: calls.append(request) or httpx.Response(200, json=RECEIPT)) as client,
        pytest.raises(ValueError),
    ):
        client.request_linked_verification(RUN, body)
    assert calls == []


@pytest.mark.parametrize("omitted", ACKNOWLEDGMENTS + ["request_id", "candidate_digest"])
def test_cli_requires_each_explicit_flag_without_prompt_or_request(monkeypatch, omitted):
    calls = []
    body = {key: value for key, value in BODY.items() if key != omitted}
    args = ["linked-verifications", "request", RUN]
    for field in ("request_id", "candidate_digest"):
        if field in body:
            args += ["--" + field.replace("_", "-"), body[field]]
    args += ["--" + key.replace("_", "-") for key in ACKNOWLEDGMENTS if key in body]
    with connection(lambda request: calls.append(request) or httpx.Response(200, json=RECEIPT)) as client:
        bind(monkeypatch, client)
        result = CliRunner().invoke(cli.main, args, input="")
    assert result.exit_code == 2
    assert "--" + omitted.replace("_", "-") in result.output
    assert calls == []


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
@pytest.mark.parametrize("operation", ["candidate", "request", "status"])
def test_foreign_path_is_rejected_before_transport(monkeypatch, interface, operation):
    calls = []
    with connection(lambda request: calls.append(request) or httpx.Response(200, json=RECEIPT)) as client:
        bind(monkeypatch, client)
        with pytest.raises((ValueError, ToolError)) as failure:
            invoke(interface, operation, client, run_id="../private-route-canary")
    assert calls == [] and "private-route-canary" not in str(failure.value)


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
@pytest.mark.parametrize("http_status", [401, 403, 404, 409, 500])
def test_refused_or_stale_admission_never_retries_or_replaces_identity(monkeypatch, interface, http_status):
    calls = []
    with connection(
        lambda request: calls.append(request) or httpx.Response(http_status, json={"detail": "Refused"})
    ) as client:
        bind(monkeypatch, client)
        with pytest.raises((RuntimeError, ValueError, ToolError)):
            invoke(interface, "request", client)
    assert len(calls) == 1 and json.loads(calls[0].content) == BODY


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_lost_post_reply_uses_only_explicit_same_identity_readback(monkeypatch, interface):
    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("Controlled uncertain write", request=request)
        return httpx.Response(200, json=RECEIPT)

    with connection(handle) as client:
        bind(monkeypatch, client)
        with pytest.raises((httpx.ReadTimeout, ValueError, ToolError)):
            invoke(interface, "request", client)
        assert len(calls) == 1
        assert invoke(interface, "status", client) == RECEIPT
    assert [request.method for request in calls] == ["POST", "GET"]
    assert calls[1].url.path.endswith("/" + REQUEST)
    assert json.loads(calls[0].content) == BODY


@pytest.mark.parametrize(
    "operation,mutation",
    [
        ("candidate", "foreign_run"),
        ("candidate", "digest"),
        ("request", "foreign_run"),
        ("request", "digest"),
        ("request", "request"),
        ("request", "stale_candidate"),
        ("request", "settlement"),
        ("status", "foreign_run"),
        ("status", "request"),
        ("status", "digest"),
        ("status", "settlement"),
    ],
)
def test_sdk_rejects_foreign_or_malformed_response_without_transport_retry(operation, mutation):
    if operation == "candidate":
        response = copy.deepcopy(CANDIDATE)
        if mutation == "foreign_run":
            response["run_id"] = "f" * 32
            response["digest"] = digest({key: value for key, value in response.items() if key != "digest"})
        else:
            response["digest"] = "0" * 64
    else:
        response = receipt(CANDIDATE, status="completed")
        if mutation == "foreign_run":
            response["run_id"] = "f" * 32
            response["candidate"]["run_id"] = "f" * 32
        elif mutation == "request":
            response["request_id"] = response["request"]["request_id"] = "f" * 32
        elif mutation == "settlement":
            response["settlement"]["commands_digest"] = "0" * 64
        else:
            response["candidate"]["runtime_contract"] = "0" * 64
        if mutation != "settlement":
            response["candidate"]["digest"] = digest(
                {key: value for key, value in response["candidate"].items() if key != "digest"}
            )
            response["request"]["candidate_digest"] = response["candidate"]["digest"]
            response["request_digest"] = request_digest(response["request"])
        if mutation == "digest":
            response["request_digest"] = "0" * 64
    calls = []
    with (
        connection(lambda request: calls.append(request) or httpx.Response(200, json=response)) as client,
        pytest.raises(ValueError),
    ):
        invoke("sdk", operation, client)
    assert len(calls) == 1


def test_sdk_validates_native_model_and_preserves_explicit_same_request():
    calls = []
    with connection(lambda request: calls.append(request) or httpx.Response(200, json=RECEIPT)) as client:
        native = LinkedVerificationInputV1.model_validate(BODY)
        assert client.request_linked_verification(RUN, native) == RECEIPT
        assert client.request_linked_verification(RUN, native) == RECEIPT
    assert len(calls) == 2
    assert json.loads(calls[0].content) == json.loads(calls[1].content) == BODY


def test_sdk_preserves_native_fresh_check_count_beyond_legacy_budget():
    reviewed = candidate(effect_budget=51)
    body = consent(reviewed)
    response = receipt(reviewed, status="completed")
    calls = []
    with connection(lambda request: calls.append(request) or httpx.Response(200, json=response)) as client:
        assert client.request_linked_verification(RUN, body) == response
    assert len(calls) == 1
    assert response["settlement"]["checks_count"] == 51
    assert response["candidate"]["original_effect_budget"] == 22
    assert response["candidate"]["original_effect_count"] == 18


def test_noninteractive_cli_uses_configured_endpoint_and_bearer(monkeypatch):
    calls = []
    native_client = httpx.Client

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=RECEIPT)

    def configured_transport(*args, **kwargs):
        return native_client(*args, **kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setenv("FORGE_URL", "https://configured.invalid")
    monkeypatch.setenv("FORGE_API_TOKEN", "fictional-configured-token")
    monkeypatch.setattr(httpx, "Client", configured_transport)
    args = [
        "linked-verifications",
        "request",
        RUN,
        "--request-id",
        REQUEST,
        "--candidate-digest",
        BODY["candidate_digest"],
        *["--" + key.replace("_", "-") for key in ACKNOWLEDGMENTS],
    ]
    result = CliRunner().invoke(cli.main, args, input="")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == RECEIPT
    assert len(calls) == 1
    assert calls[0].url.host == "configured.invalid"
    assert calls[0].headers["Authorization"] == "Bearer fictional-configured-token"
    assert json.loads(calls[0].content) == BODY


def test_cli_without_configured_token_refuses_before_transport(monkeypatch):
    calls = []
    monkeypatch.delenv("FORGE_API_TOKEN", raising=False)
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: calls.append((args, kwargs)))
    result = CliRunner().invoke(cli.main, ["linked-verifications", "candidate", RUN], input="")
    assert result.exit_code == 1
    assert isinstance(result.exception, ValueError)
    assert "operator token is required" in str(result.exception)
    assert calls == []


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_status_rejects_foreign_request_path_before_transport(monkeypatch, interface):
    calls = []
    with connection(lambda request: calls.append(request) or httpx.Response(200, json=RECEIPT)) as client:
        bind(monkeypatch, client)
        with pytest.raises((ValueError, ToolError)):
            invoke(interface, "status", client, request_id="../foreign-request")
    assert calls == []


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_status_refuses_contradictory_native_progress(monkeypatch, interface):
    response = copy.deepcopy(RECEIPT)
    response["progress"]["next_ordinal"] = 1
    calls = []
    with connection(lambda request: calls.append(request) or httpx.Response(200, json=response)) as client:
        bind(monkeypatch, client)
        with pytest.raises((ValueError, ToolError)):
            invoke(interface, "status", client)
    assert len(calls) == 1 and calls[0].method == "GET"
