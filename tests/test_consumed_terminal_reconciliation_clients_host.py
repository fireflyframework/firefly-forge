# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""All reconciliation adapters bind one reviewed action and never retry transport."""

import asyncio
import contextlib
import copy
import json

import httpx
import pytest
from click.testing import CliRunner
from mcp.server.mcpserver.exceptions import ToolError
from test_consumed_terminal_clients_host import RECEIPT, REQUEST, RUN
from test_original_selection_clients_host import connection

from firefly_forge import cli
from firefly_forge.journal import digest
from firefly_forge.mcp_server import create_server

CANDIDATE = dict(
    version=1,
    run_id=RUN,
    request_id=REQUEST,
    same_task_attempt=2,
    **{
        key: str(i % 10) * 64
        for i, key in enumerate(
            (
                "owner_digest",
                "actor_digest",
                "policy_digest",
                "runtime_contract",
                "profile_digest",
                "run_digest",
                "task_digest",
                "journal_digest",
                "recovery_digest",
                "claim_digest",
                "source_digest",
                "index_digest",
            )
        )
    },
)
CANDIDATE["recovery_digest"] = RECEIPT["record_digest"]
CANDIDATE["digest"] = digest(CANDIDATE)
BODY = dict(
    version=1,
    candidate_digest=CANDIDATE["digest"],
    acknowledge_same_claimed_action=True,
    acknowledge_no_execution_grant=True,
    acknowledge_local_postprocessing=True,
)
RECONCILIATION = dict(version=1, candidate=CANDIDATE, request=BODY, record_digest="f" * 64, settlement=RECEIPT)


def invoke(interface, operation, client, body=BODY):
    if interface == "sdk":
        if operation == "reconciliation-candidate":
            return client.consumed_terminal_reconciliation_candidate(RUN, REQUEST)
        if operation == "reconciliation-status":
            return client.consumed_terminal_reconciliation_status(RUN, REQUEST)
        return client.reconcile_consumed_terminal(RUN, REQUEST, body)
    if interface == "cli":
        args = ["consumed-terminal-completions", operation, RUN, REQUEST]
        if operation == "reconcile":
            args += ["--candidate-digest", body["candidate_digest"]]
            args += ["--" + key.replace("_", "-") for key in body if key.startswith("acknowledge_")]
        response = CliRunner().invoke(cli.main, args)
        if response.exit_code:
            raise response.exception or ValueError(response.output)
        return json.loads(response.output)
    tool = {
        "reconciliation-candidate": "get_consumed_terminal_reconciliation_candidate",
        "reconciliation-status": "get_consumed_terminal_reconciliation_status",
        "reconcile": "reconcile_consumed_terminal",
    }[operation]
    arguments = dict(identifier=RUN, request_id=REQUEST)
    if operation == "reconcile":
        arguments["request"] = body
    result = asyncio.run(create_server().call_tool(tool, arguments))
    if result.is_error:
        raise ValueError("Rejected reconciliation tool")
    return result.structured_content


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
@pytest.mark.parametrize("operation", ["reconciliation-candidate", "reconcile", "reconciliation-status"])
def test_reconciliation_route_and_strict_scope(interface, operation, monkeypatch):
    calls = []
    result = CANDIDATE if operation == "reconciliation-candidate" else RECONCILIATION
    with connection(lambda req: calls.append(req) or httpx.Response(200, json=result)) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        assert invoke(interface, operation, client) == result
    suffix = {
        "reconciliation-candidate": "reconciliation-candidate",
        "reconcile": "reconcile",
        "reconciliation-status": "reconciliation",
    }[operation]
    assert len(calls) == 1
    assert calls[0].url.path == f"/api/v1/runs/{RUN}/worker-completion-settlements/{REQUEST}/{suffix}"
    assert calls[0].headers["Authorization"] == "Bearer fictional-operator-token"
    assert calls[0].method == ("POST" if operation == "reconcile" else "GET")
    if operation == "reconcile":
        assert json.loads(calls[0].content) == BODY


@pytest.mark.parametrize("interface", ["sdk", "mcp"])
@pytest.mark.parametrize(
    "mutation",
    [{"version": True}, {"acknowledge_same_claimed_action": 1}, {"candidate_digest": "A" * 64}, {"extra": True}],
)
def test_reconciliation_strict_input_before_transport(interface, mutation, monkeypatch):
    calls = []
    with connection(lambda req: calls.append(req) or httpx.Response(200, json=RECONCILIATION)) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        with pytest.raises((ValueError, ToolError)):
            invoke(interface, "reconcile", client, BODY | mutation)
    assert calls == []


@pytest.mark.parametrize("interface", ["sdk", "cli", "mcp"])
def test_unknown_reconciliation_delivery_never_retries(interface, monkeypatch):
    calls = []

    def uncertain(request):
        calls.append(request)
        raise httpx.ReadTimeout("Controlled uncertain delivery")

    with connection(uncertain) as client:
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        with pytest.raises((ValueError, httpx.ReadTimeout, ToolError)):
            invoke(interface, "reconcile", client)
    assert len(calls) == 1


def test_sdk_rejects_cross_action_reconciliation_receipt():
    result = copy.deepcopy(RECONCILIATION)
    result["settlement"]["request_id"] = "d" * 32
    with connection(lambda req: httpx.Response(200, json=result)) as client, pytest.raises(ValueError):
        client.consumed_terminal_reconciliation_status(RUN, REQUEST)


def test_reconciliation_module_bytes_belong_to_every_admission_hash(monkeypatch):
    from pathlib import Path

    from firefly_forge.agentic_checkpoint_invocation import runtime_contract
    from firefly_forge.known_worker_completions import policy_digest
    from firefly_forge.source_budget_allowances import admission_contract_digest

    before = [runtime_contract(), policy_digest(), admission_contract_digest()]
    read = Path.read_bytes

    def changed(path):
        value = read(path)
        return (
            value + b"\n# controlled private byte drift\n"
            if path.name == "consumed_terminal_reconciliations.py"
            else value
        )

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert [
        runtime_contract() != before[0],
        policy_digest() != before[1],
        admission_contract_digest() != before[2],
    ] == [True, True, True]


async def test_public_hash_admission_guards_remain_closed_inside_native_unit():
    from pyfly.data.transaction import TransactionTemplate
    from test_git_delivery_remote_uow_host import MemoryManager

    from firefly_forge.agentic_checkpoint_invocation import _runtime_material_digest, runtime_contract
    from firefly_forge.known_worker_completions import _policy_material_digest, policy_digest

    expected = runtime_contract(), policy_digest()
    async with TransactionTemplate(MemoryManager({})).transaction():
        with pytest.raises(ValueError, match="no active caller transaction"):
            runtime_contract()
        with pytest.raises(ValueError, match="no active caller transaction"):
            policy_digest()
        assert (_runtime_material_digest(), _policy_material_digest()) == expected
