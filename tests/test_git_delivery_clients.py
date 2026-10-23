# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""SDK/CLI preserve exact bounded policy and checkpoint identities without retries."""

import json

import httpx
import pytest
from click.testing import CliRunner
from test_original_recovery_sdk import connection

from firefly_forge import cli

PROJECT, RUN, REQUEST = (c * 32 for c in "abc")
POLICY = {
    "version": 1,
    "request_id": REQUEST,
    "expected_previous_digest": None,
    "policy": {
        "version": 1,
        "enabled": True,
        "every_units": 2,
        "on_pause": True,
        "remote": "local",
        "max_checkpoints": 50,
    },
}
CHECKPOINT = {"version": 1, "request_id": REQUEST, "expected_head": "d" * 40, "expected_source_tree": "e" * 40}


def test_sdk_exact_methods_payloads_and_read_only_observation():
    calls = []
    with connection(lambda request: calls.append(request) or httpx.Response(200, json={"retained": True})) as client:
        client.git_delivery_policy(PROJECT)
        client.configure_git_deliveries(PROJECT, POLICY)
        client.git_deliveries(RUN)
        client.request_git_delivery(RUN, CHECKPOINT)
        client.git_delivery_request(RUN, REQUEST)
        client.git_delivery_observations(RUN, REQUEST)
    assert [(r.method, r.url.path) for r in calls] == [
        ("GET", f"/api/v1/projects/{PROJECT}/git-delivery-policy"),
        ("PUT", f"/api/v1/projects/{PROJECT}/git-delivery-policy"),
        ("GET", f"/api/v1/runs/{RUN}/git-deliveries"),
        ("POST", f"/api/v1/runs/{RUN}/git-deliveries"),
        ("GET", f"/api/v1/runs/{RUN}/git-deliveries/requests/{REQUEST}"),
        ("GET", f"/api/v1/runs/{RUN}/git-deliveries/observations/{REQUEST}"),
    ]
    assert json.loads(calls[1].content) == POLICY and json.loads(calls[3].content) == CHECKPOINT


@pytest.mark.parametrize("field,value", [("expected_head", "../other"), ("request_id", "ABC"), ("version", True)])
def test_invalid_checkpoint_is_refused_before_any_http(field, value):
    calls = []
    with (
        connection(lambda request: calls.append(request) or httpx.Response(200, json={})) as client,
        pytest.raises(ValueError),
    ):
        client.request_git_delivery(RUN, {**CHECKPOINT, field: value})
    assert calls == []


def test_unknown_post_is_sent_once_and_never_replaced():
    calls = []

    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("Controlled public fixture", request=request)

    with connection(timeout) as client, pytest.raises(httpx.ReadTimeout):
        client.request_git_delivery(RUN, CHECKPOINT)
    assert len(calls) == 1 and json.loads(calls[0].content) == CHECKPOINT


def test_cli_uses_typed_api_for_policy_request_receipt_and_observation(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        cli, "client", lambda: connection(lambda request: calls.append(request) or httpx.Response(200, json={}))
    )
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(POLICY))
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps(CHECKPOINT))
    runner = CliRunner()
    for args in [
        ["policy", PROJECT],
        ["configure", PROJECT, str(policy)],
        ["list", RUN],
        ["request", RUN, str(checkpoint)],
        ["receipt", RUN, REQUEST],
        ["observe", RUN, REQUEST],
    ]:
        result = runner.invoke(cli.main, ["git-deliveries", *args])
        assert result.exit_code == 0, result.output
    assert len(calls) == 6 and [r.method for r in calls] == ["GET", "PUT", "GET", "POST", "GET", "GET"]
