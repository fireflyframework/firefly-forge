# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Operator transport preserves the reviewed task and request, including lost-response handling."""

import json

import httpx
import pytest
from click.testing import CliRunner
from test_original_recovery_sdk import connection

from firefly_forge import cli

RUN, REQUEST = "a" * 32, "b" * 32
BODY = {
    "request_id": REQUEST,
    "candidate_digest": "c" * 64,
    "acknowledge_environment_only": True,
    "acknowledge_resume_existing_task": True,
}


def test_sdk_exact_environment_review_admission_and_receipt_have_no_budget_override():
    calls = []

    def reply(request):
        calls.append((request.method, request.url.path, request.content))
        return httpx.Response(200, json={"retained": BODY})

    with connection(reply) as client:
        assert client.workspace_reconciliation_candidate(RUN) == {"retained": BODY}
        assert client.reconcile_workspace(RUN, BODY) == {"retained": BODY}
        assert client.workspace_reconciliation(RUN, REQUEST) == {"retained": BODY}
        for invalid in (BODY | {"attempt": 3}, BODY | {"acknowledge_environment_only": 1}):
            with pytest.raises(ValueError):
                client.reconcile_workspace(RUN, invalid)
    assert [(m, p) for m, p, _ in calls] == [
        ("GET", f"/api/v1/runs/{RUN}/workspace-reconciliations/candidate"),
        ("POST", f"/api/v1/runs/{RUN}/workspace-reconciliations"),
        ("GET", f"/api/v1/runs/{RUN}/workspace-reconciliations/{REQUEST}"),
    ]
    assert json.loads(calls[1][2]) == BODY


def test_uncertain_transport_submits_once_and_never_allocates_another_identity():
    calls = []

    def lost(request):
        calls.append(request)
        raise httpx.ReadTimeout("Controlled lost environment receipt")

    with connection(lost) as client, pytest.raises(httpx.ReadTimeout):
        client.reconcile_workspace(RUN, BODY)
    assert len(calls) == 1 and json.loads(calls[0].content) == BODY


def test_cli_submits_the_strict_original_document_once(monkeypatch, tmp_path):
    calls = []

    def reply(request):
        calls.append(request)
        return httpx.Response(201, json={"retained": BODY})

    monkeypatch.setattr(cli, "client", lambda: connection(reply))
    document = tmp_path / "reviewed-environment.json"
    document.write_text(json.dumps(BODY))
    result = CliRunner().invoke(cli.main, ["workspace-reconciliations", "admit", RUN, str(document)])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1 and json.loads(calls[0].content) == BODY
