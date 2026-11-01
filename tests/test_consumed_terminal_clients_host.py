# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Public V2 adapters bind the reviewed result and never retry an uncertain write."""

import asyncio
import contextlib
import copy
import json

import httpx
import pytest
from click.testing import CliRunner
from mcp.server.mcpserver.exceptions import ToolError
from test_original_selection_clients_host import connection

from firefly_forge import cli
from firefly_forge.journal import digest
from firefly_forge.mcp_server import create_server

RUN, REQUEST = "a" * 32, "b" * 32
PROOF = dict(
    version=2,
    run_id=RUN,
    task_id="implement",
    task_attempt=2,
    task_key="c" * 32,
    completion_count=1,
    completions=[
        dict(
            request_id="fbr1_" + "d" * 32,
            intent_digest="1" * 64,
            checkpoint_record_digest="2" * 64,
            response_record_digest="3" * 64,
            consume_record_digest="4" * 64,
        )
    ],
    terminal_report_sha256="5" * 64,
    final_response_sha256="6" * 64,
    recorded_runtime_contract="7" * 64,
    current_runtime_contract="8" * 64,
    current_profile_digest="9" * 64,
    source_digest="a" * 64,
    parent_final_digest="b" * 64,
    handoff_custody_digest="c" * 64,
    usage_before_digest="d" * 64,
    usage_after_digest="e" * 64,
    verification="consumed_terminal_output_known_only",
)
PROOF["digest"] = digest(PROOF)
CANDIDATE = dict(
    version=2,
    run_id=RUN,
    actor_digest="1" * 64,
    policy_digest="2" * 64,
    run_digest="3" * 64,
    journal_digest="4" * 64,
    proof=PROOF,
)
CANDIDATE["digest"] = digest(CANDIDATE)
BODY = dict(
    version=2,
    candidate_digest=CANDIDATE["digest"],
    policy_digest=CANDIDATE["policy_digest"],
    expected_run_digest=CANDIDATE["run_digest"],
    journal_digest=CANDIDATE["journal_digest"],
    proof_digest=PROOF["digest"],
    acknowledge_known_worker_output=True,
    acknowledge_no_execution_grant=True,
    acknowledge_local_postprocessing=True,
)
RECEIPT = dict(
    version=2,
    kind="consumed_terminal_recovery",
    run_id=RUN,
    request_id=REQUEST,
    record_digest="e" * 64,
    candidate=CANDIDATE,
    request=BODY,
    state="queued",
    run_status="failed",
    same_task_attempt=2,
    no_execution_grant=True,
)
TOOLS = {
    "candidate": "get_consumed_terminal_candidate",
    "request": "request_consumed_terminal",
    "status": "get_consumed_terminal_status",
}
ACKS = [key for key in BODY if key.startswith("acknowledge_")]
HASHES = [key for key in BODY if key.endswith("_digest")]


def invoke(interface, operation, client, body=BODY, run=RUN, request_id=REQUEST, omitted_ack=None):
    if interface == "sdk":
        if operation == "candidate":
            return client.consumed_terminal_candidate(run)
        if operation == "status":
            return client.consumed_terminal_status(run, request_id)
        return client.request_consumed_terminal(run, request_id, body)
    if interface == "cli":
        arguments = ["consumed-terminal-completions", operation, run]
        if operation in {"request", "status"}:
            arguments.append(request_id)
        if operation == "request":
            for key in HASHES:
                arguments.extend(["--" + key.replace("_", "-"), body[key]])
            arguments.extend("--" + key.replace("_", "-") for key in ACKS if key != omitted_ack)
        result = CliRunner().invoke(cli.main, arguments)
        if result.exit_code:
            raise result.exception or AssertionError(result.output)
        return json.loads(result.output)
    arguments = dict(identifier=run)
    if operation != "candidate":
        arguments["request_id"] = request_id
    if operation == "request":
        arguments["request"] = body
    result = asyncio.run(create_server().call_tool(TOOLS[operation], arguments))
    if result.is_error:
        raise ValueError("Rejected consumed-terminal tool input or response")
    return result.structured_content


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
@pytest.mark.parametrize("operation", ["candidate", "request", "status"])
def test_exact_v2_authenticated_route_body_and_readback(interface, operation, monkeypatch):
    calls = []
    response = CANDIDATE if operation == "candidate" else RECEIPT
    original = copy.deepcopy(BODY)
    with connection(lambda request: calls.append(request) or httpx.Response(200, json=response)) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        assert invoke(interface, operation, client) == response
    suffix = "candidate" if operation == "candidate" else REQUEST
    assert len(calls) == 1 and calls[0].url.path == f"/api/v1/runs/{RUN}/worker-completion-settlements/{suffix}"
    assert calls[0].url.query == b""
    assert calls[0].method == ("POST" if operation == "request" else "GET")
    assert calls[0].headers["Authorization"] == "Bearer fictional-operator-token"
    assert (json.loads(calls[0].content) == BODY) if operation == "request" else calls[0].content == b""
    assert original == BODY


@pytest.mark.parametrize("interface", ["sdk", "mcp"])
@pytest.mark.parametrize(
    "mutation",
    [
        {"version": 1},
        {"version": 2.0},
        {"version": True},
        {"version": "2"},
        {"acknowledge_known_worker_output": 1},
        {"acknowledge_no_execution_grant": False},
        {"acknowledge_local_postprocessing": "true"},
        {"proof_digest": "F" * 64},
        {"feedback_record_digest": "f" * 64},
        {"endpoint": "https://unreviewed.invalid"},
    ],
)
def test_v2_strict_body_refuses_before_transport(interface, mutation, monkeypatch):
    calls = []
    with connection(lambda request: calls.append(request) or httpx.Response(200, json=RECEIPT)) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        with pytest.raises((ValueError, ToolError)):
            invoke(interface, "request", client, BODY | mutation)
    assert calls == []


@pytest.mark.parametrize("ack", ACKS)
def test_cli_requires_each_explicit_acknowledgment(ack, monkeypatch):
    calls = []
    with connection(lambda request: calls.append(request) or httpx.Response(200, json=RECEIPT)) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        with pytest.raises(SystemExit):
            invoke("cli", "request", client, omitted_ack=ack)
    assert calls == []


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
@pytest.mark.parametrize("operation", ["candidate", "request", "status"])
def test_no_path_substitution_reaches_transport(interface, operation, monkeypatch):
    calls = []
    with connection(lambda request: calls.append(request) or httpx.Response(200, json=RECEIPT)) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        with pytest.raises((ValueError, ToolError)):
            invoke(interface, operation, client, run="../other")
    assert calls == []


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_uncertain_post_is_sent_once_without_automatic_readback(interface, monkeypatch):
    calls = []

    def lost(request):
        calls.append(request)
        raise httpx.ReadTimeout("Controlled unknown delivery", request=request)

    with connection(lost) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        with pytest.raises((httpx.ReadTimeout, ValueError, ToolError)):
            invoke(interface, "request", client)
    assert len(calls) == 1 and calls[0].method == "POST"


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
@pytest.mark.parametrize("status", [403, 409, 429, 500])
def test_server_denial_never_retries_or_changes_request_identity(interface, status, monkeypatch):
    calls = []
    with connection(
        lambda request: calls.append(request) or httpx.Response(status, json={"detail": "Refused"})
    ) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        with pytest.raises((RuntimeError, ValueError, ToolError)):
            invoke(interface, "request", client)
    assert len(calls) == 1 and json.loads(calls[0].content) == BODY


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
@pytest.mark.parametrize("operation", ["candidate", "request", "status"])
@pytest.mark.parametrize("response", [{"version": 1}, {"version": 2, "terminal_report": "PRIVATE-RESPONSE-MARKER"}])
def test_shared_v1_or_malformed_response_is_not_relabelled_v2(interface, operation, response, monkeypatch):
    calls = []
    with connection(lambda request: calls.append(request) or httpx.Response(200, json=response)) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        with pytest.raises((ValueError, ToolError)) as error:
            invoke(interface, operation, client)
    assert "PRIVATE-RESPONSE-MARKER" not in str(error.value)
    assert len(calls) == 1


async def test_mcp_v2_read_mutation_annotations_and_strict_request_schema():
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    for operation, name in TOOLS.items():
        tool = tools[name]
        assert tool.annotations.read_only_hint is (operation != "request")
        assert tool.annotations.idempotent_hint is True
    schema = tools[TOOLS["request"]].input_schema["properties"]["request"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["version"]["const"] == 2
    assert set(schema["required"]) == set(BODY)


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
@pytest.mark.parametrize("operation", ["request", "status"])
def test_request_identity_is_validated_before_transport(interface, operation, monkeypatch):
    calls = []
    with connection(lambda request: calls.append(request) or httpx.Response(200, json=RECEIPT)) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        with pytest.raises((ValueError, ToolError)):
            invoke(interface, operation, client, request_id="../replacement")
    assert calls == []


def response_for_another_run(operation):
    candidate = copy.deepcopy(CANDIDATE)
    candidate["run_id"] = "f" * 32
    candidate["proof"]["run_id"] = "f" * 32
    candidate["proof"]["digest"] = digest({key: value for key, value in candidate["proof"].items() if key != "digest"})
    candidate["digest"] = digest({key: value for key, value in candidate.items() if key != "digest"})
    if operation == "candidate":
        return candidate
    response = copy.deepcopy(RECEIPT)
    response["run_id"] = candidate["run_id"]
    response["candidate"] = candidate
    response["request"]["candidate_digest"] = candidate["digest"]
    response["request"]["proof_digest"] = candidate["proof"]["digest"]
    return response


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
@pytest.mark.parametrize("operation", ["candidate", "request", "status"])
def test_valid_foreign_run_response_is_not_disclosed(interface, operation, monkeypatch):
    response = response_for_another_run(operation)
    calls = []
    with connection(lambda request: calls.append(request) or httpx.Response(200, json=response)) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        with pytest.raises((ValueError, ToolError)):
            invoke(interface, operation, client)
    assert len(calls) == 1


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
@pytest.mark.parametrize("operation", ["request", "status"])
def test_valid_receipt_for_different_request_is_refused(interface, operation, monkeypatch):
    response = {**RECEIPT, "request_id": "f" * 32}
    with connection(lambda request: httpx.Response(200, json=response)) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        with pytest.raises((ValueError, ToolError)):
            invoke(interface, operation, client)


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_valid_same_run_receipt_cannot_substitute_reviewed_request(interface, monkeypatch):
    response = copy.deepcopy(RECEIPT)
    response["candidate"]["journal_digest"] = "f" * 64
    response["candidate"]["digest"] = digest(
        {key: value for key, value in response["candidate"].items() if key != "digest"}
    )
    response["request"]["candidate_digest"] = response["candidate"]["digest"]
    response["request"]["journal_digest"] = response["candidate"]["journal_digest"]
    calls = []
    with connection(lambda request: calls.append(request) or httpx.Response(200, json=response)) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        with pytest.raises((ValueError, ToolError)):
            invoke(interface, "request", client)
    assert len(calls) == 1


def test_sdk_accepts_native_typed_input_without_changing_its_values():
    from firefly_forge.known_worker_completion_models import ConsumedTerminalInputV2

    request = ConsumedTerminalInputV2.model_validate(BODY)
    calls = []
    with connection(lambda call: calls.append(call) or httpx.Response(200, json=RECEIPT)) as client:
        assert client.request_consumed_terminal(RUN, REQUEST, request) == RECEIPT
    assert json.loads(calls[0].content) == request.model_dump() == BODY


def test_sdk_revalidates_constructed_models_instead_of_trusting_the_type():
    from firefly_forge.known_worker_completion_models import ConsumedTerminalInputV2

    request = ConsumedTerminalInputV2.model_construct(**(BODY | {"acknowledge_local_postprocessing": 1}))
    calls = []
    with (
        connection(lambda call: calls.append(call) or httpx.Response(200, json=RECEIPT)) as client,
        pytest.raises(ValueError),
    ):
        client.request_consumed_terminal(RUN, REQUEST, request)
    assert calls == []
