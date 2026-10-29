# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Operator adapters submit once and inspect uncertain delivery using the saved identity."""

import asyncio
import contextlib
import json

import httpx
import pytest
from click.testing import CliRunner
from mcp.server.mcpserver.exceptions import ToolError
from test_original_selection_clients_host import connection

from firefly_forge import cli
from firefly_forge.mcp_server import create_server

RUN, REQUEST, ACTION = "a" * 32, "b" * 32, "c" * 32
BODY = dict(
    version=1,
    candidate_digest="1" * 64,
    valid_for_seconds=60,
    acknowledge_already_consumed_final_output=True,
    acknowledge_no_completed_effect_replay=True,
    acknowledge_same_request_attempt_and_budgets=True,
    acknowledge_finite_ownership_lease=True,
)


def invoke(interface, operation, client, tmp_path, body=BODY):
    if interface == "sdk":
        if operation == "candidate":
            return client.final_outcome_continuation_candidate(RUN, REQUEST)
        if operation == "status":
            return client.final_outcome_continuation_status(RUN, REQUEST)
        return client.request_final_outcome_continuation(RUN, REQUEST, ACTION, body)
    if interface == "cli":
        args = ["final-outcome-continuations", operation, RUN, REQUEST]
        if operation == "request":
            path = tmp_path / "reviewed-action.json"
            path.write_text(json.dumps(body))
            args += [ACTION, str(path)]
        result = CliRunner().invoke(cli.main, args)
        if result.exit_code:
            raise result.exception or AssertionError(result.output)
        return json.loads(result.output)
    tool = {
        "candidate": "get_final_outcome_candidate",
        "status": "get_final_outcome_status",
        "request": "request_final_outcome_continuation",
    }[operation]
    args = dict(identifier=RUN, request_id=REQUEST)
    if operation == "request":
        args.update(action_id=ACTION, request=body)
    result = asyncio.run(create_server().call_tool(tool, args))
    if result.is_error:
        raise ValueError("Rejected final-outcome tool input")
    return result.structured_content


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
@pytest.mark.parametrize("operation", ["candidate", "request", "status"])
def test_final_outcome_adapters_forward_one_exact_authenticated_call(interface, operation, tmp_path, monkeypatch):
    calls = []
    record = dict(run_id=RUN, request_id=REQUEST, action_id=ACTION, status="queued")
    with connection(lambda r: calls.append(r) or httpx.Response(200, json=record)) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        assert invoke(interface, operation, client, tmp_path) == record
    suffix = "/actions/" + ACTION if operation == "request" else "/" + operation
    assert (
        len(calls) == 1
        and calls[0].url.path == f"/api/v1/runs/{RUN}/final-outcome-continuations/requests/{REQUEST}" + suffix
    )
    assert calls[0].method == ("POST" if operation == "request" else "GET")
    assert calls[0].headers["Authorization"] == "Bearer fictional-operator-token"
    assert json.loads(calls[0].content) == BODY if operation == "request" else calls[0].content == b""


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_final_outcome_uncertain_submission_is_never_repeated(interface, tmp_path, monkeypatch):
    calls = []

    def lost(request):
        calls.append(request)
        raise httpx.ReadTimeout("Controlled uncertain saved action", request=request)

    with connection(lost) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        with pytest.raises((httpx.ReadTimeout, ValueError, ToolError)):
            invoke(interface, "request", client, tmp_path)
    assert len(calls) == 1 and calls[0].method == "POST"


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_final_outcome_nonliteral_consent_stops_before_transport(interface, tmp_path, monkeypatch):
    calls = []
    with connection(lambda r: calls.append(r) or httpx.Response(200, json={})) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        with pytest.raises((ValueError, ToolError)):
            invoke(interface, "request", client, tmp_path, {**BODY, "acknowledge_already_consumed_final_output": 1})
    assert calls == []
