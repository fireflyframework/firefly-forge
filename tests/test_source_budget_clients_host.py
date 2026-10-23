# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Operator recovery preserves finite authority and never retries an uncertain write."""

import json

import httpx
import pytest
from click.testing import CliRunner
from test_original_recovery_sdk import connection

from firefly_forge import cli

RUN, PARENT, REQUEST = (character * 32 for character in "abc")
PROPOSAL = {"version": 1, "read_file_lifetime_limit": 20, "activation_ceiling": 5, "additional_activations": 1}
BODY = {
    "version": 1,
    "request_id": REQUEST,
    "candidate_digest": "d" * 64,
    "admission_contract_digest": "e" * 64,
    "previous_catalog_path": "/owned/model.json",
    "valid_for_seconds": 120,
    "proposal": PROPOSAL,
    "acknowledge_lifetime_history_preserved": True,
    "acknowledge_original_commands_and_final_reserve": True,
    "acknowledge_one_new_canonical_activation": True,
    "acknowledge_no_replay": True,
}
POLICY = {
    "enabled": True,
    "max_read_file_lifetime_limit": 20,
    "max_activation_ceiling": 5,
    "max_additional_activations": 1,
}
PREFIX = f"/api/v1/runs/{RUN}/source-edit-continuations/{PARENT}"


def test_sdk_uses_exact_scope_parameters_and_same_receipt_readback():
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"retained": True})

    with connection(transport) as client:
        client.source_budget_policy()
        client.configure_source_budget_policy(POLICY)
        client.source_budget_candidate(RUN, PARENT, "/owned/model.json", PROPOSAL)
        client.admit_source_budget(RUN, PARENT, BODY)
        client.source_budget_receipt(RUN, PARENT, REQUEST)
    assert [(r.method, r.url.path) for r in calls] == [
        ("GET", "/api/v1/source-budget/policy"),
        ("PUT", "/api/v1/source-budget/policy"),
        ("GET", PREFIX + "/budget-allowance-candidate"),
        ("POST", PREFIX + "/budget-allowances"),
        ("GET", PREFIX + "/budget-allowances/" + REQUEST),
    ]
    assert json.loads(calls[1].content) == POLICY
    assert dict(calls[2].url.params) == {
        "previous_catalog_path": "/owned/model.json",
        "read_file_lifetime_limit": "20",
        "activation_ceiling": "5",
        "additional_activations": "1",
    }
    assert json.loads(calls[3].content) == BODY


@pytest.mark.parametrize(
    "mutation",
    [
        {"acknowledge_no_replay": False},
        {"acknowledge_no_replay": 1},
        {"command_limit": 99},
        {"proposal": PROPOSAL | {"read_file_lifetime_limit": True}},
        {"proposal": PROPOSAL | {"additional_activations": 2}},
        {"proposal": PROPOSAL | {"activation_ceiling": 33}},
    ],
)
def test_sdk_refuses_changed_or_unbounded_authority_before_http(mutation):
    calls = []
    with connection(lambda r: calls.append(r) or httpx.Response(200, json={})) as client, pytest.raises(ValueError):
        client.admit_source_budget(RUN, PARENT, BODY | mutation)
    assert calls == []


@pytest.mark.parametrize(
    "run,parent,request_id", [("../runs", PARENT, REQUEST), (RUN, "ABC", REQUEST), (RUN, PARENT, "../other")]
)
def test_sdk_rejects_scope_path_substitution(run, parent, request_id):
    calls = []
    with connection(lambda r: calls.append(r) or httpx.Response(200, json={})) as client, pytest.raises(ValueError):
        client.source_budget_receipt(run, parent, request_id)
    assert calls == []


def test_uncertain_post_is_sent_once_without_automatic_readback_or_replacement():
    calls = []

    def transport(request):
        calls.append(request)
        raise httpx.ReadTimeout("Public controlled timeout", request=request)

    with connection(transport) as client, pytest.raises(httpx.ReadTimeout):
        client.admit_source_budget(RUN, PARENT, BODY)
    assert len(calls) == 1 and calls[0].method == "POST" and json.loads(calls[0].content) == BODY


@pytest.mark.parametrize("status", [403, 429, 500])
def test_server_refusal_never_creates_another_write(status):
    calls = []
    with (
        connection(lambda r: calls.append(r) or httpx.Response(status, json={"detail": "Refused"})) as client,
        pytest.raises(RuntimeError),
    ):
        client.admit_source_budget(RUN, PARENT, BODY)
    assert len(calls) == 1


@pytest.mark.parametrize("catalog", ["", "  ", "\x00", "x" * 4097])
def test_invalid_catalog_is_rejected_before_http(catalog):
    calls = []
    with connection(lambda r: calls.append(r) or httpx.Response(200, json={})) as client, pytest.raises(ValueError):
        client.source_budget_candidate(RUN, PARENT, catalog, PROPOSAL)
    assert calls == []


def test_cli_exposes_explicit_review_admission_and_exact_readback(monkeypatch, tmp_path):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json={"retained": True})

    monkeypatch.setattr(cli, "client", lambda: connection(transport))
    policy = tmp_path / "policy.json"
    document = tmp_path / "allowance.json"
    policy.write_text(json.dumps(POLICY))
    document.write_text(json.dumps(BODY))
    runner = CliRunner()
    for arguments in [
        ["source-budget", "policy"],
        ["source-budget", "configure-policy", str(policy)],
        [
            "source-budget",
            "candidate",
            RUN,
            PARENT,
            "--previous-catalog-path",
            "/owned/model.json",
            "--read-file-lifetime-limit",
            "20",
            "--activation-ceiling",
            "5",
            "--additional-activations",
            "1",
        ],
        ["source-budget", "admit", RUN, PARENT, str(document)],
        ["source-budget", "get", RUN, PARENT, REQUEST],
    ]:
        result = runner.invoke(cli.main, arguments)
        assert result.exit_code == 0, result.output
    assert len(calls) == 5 and json.loads(calls[3].content) == BODY


def test_cli_candidate_requires_every_explicit_limit_before_http(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "client", lambda: calls.append(True))
    result = CliRunner().invoke(cli.main, ["source-budget", "candidate", RUN, PARENT])
    assert result.exit_code == 2 and calls == []
