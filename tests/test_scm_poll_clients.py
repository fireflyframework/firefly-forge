# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit scoped requests and read-only recovery; no provider or native database."""

import contextlib
import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from firefly_forge import cli
from firefly_forge.client import ForgeClient

VECTOR = json.loads((Path(__file__).parents[1] / "studio/tests/scm-polling-contract-fixture.json").read_text())
RECEIPT = VECTOR["receipt"]
PROJECT, REPOSITORY = RECEIPT["project_id"], RECEIPT["repository_id"]
BASE = f"/api/v1/projects/{PROJECT}/repositories/{REPOSITORY}/scm-polling"


@pytest.mark.parametrize("interface", ["sdk", "cli"])
def test_polling_configuration_and_consent_have_explicit_exact_read_recovery(monkeypatch, tmp_path, interface):
    calls = []
    configuration = dict(
        version=1,
        request_id="a" * 32,
        expected_previous_digest=None,
        expected_project_binding_digest="e" * 64,
        expected_connection_revision="b" * 32,
        enabled=True,
        interval_seconds=60,
    )

    def transport(request):
        calls.append((request.method, request.url.path, dict(request.url.params), request.content))
        return httpx.Response(200, json={"receipt": "controlled"})

    with ForgeClient("https://forge.invalid", "fictional-token") as client:
        client.http.close()
        client.http = httpx.Client(base_url="https://forge.invalid/api/v1/", transport=httpx.MockTransport(transport))
        monkeypatch.setattr(cli, "client", lambda: contextlib.nullcontext(client))
        if interface == "sdk":
            client.scm_polling_setup(PROJECT, REPOSITORY)
            client.configure_scm_polling(PROJECT, REPOSITORY, configuration)
            client.scm_polling_configuration(PROJECT, REPOSITORY, configuration["request_id"])
            client.scm_polling_status(PROJECT, REPOSITORY)
            client.scm_polling_candidates(PROJECT, REPOSITORY)
            client.configure_scm_polling_binding(PROJECT, REPOSITORY, RECEIPT["request"])
            client.scm_polling_binding(PROJECT, REPOSITORY, RECEIPT["request"]["request_id"])
            client.scm_polling_heads(PROJECT, REPOSITORY, "f" * 64)
        else:
            config_file, consent_file = tmp_path / "config.json", tmp_path / "consent.json"
            config_file.write_text(json.dumps(configuration))
            consent_file.write_text(json.dumps(RECEIPT["request"]))
            for arguments in [
                ["setup", PROJECT, REPOSITORY],
                ["configure", PROJECT, REPOSITORY, str(config_file)],
                ["configuration", PROJECT, REPOSITORY, configuration["request_id"]],
                ["status", PROJECT, REPOSITORY],
                ["candidates", PROJECT, REPOSITORY],
                ["consent", PROJECT, REPOSITORY, str(consent_file)],
                ["binding", PROJECT, REPOSITORY, RECEIPT["request"]["request_id"]],
                ["heads", PROJECT, REPOSITORY, "--after", "f" * 64],
            ]:
                result = CliRunner().invoke(cli.main, ["project", "scm-polling", *arguments])
                assert result.exit_code == 0, result.output
        assert [(method, path, params) for method, path, params, _ in calls] == [
            ("GET", BASE + "/setup", {}),
            ("POST", BASE + "/configuration", {}),
            ("GET", BASE + "/configuration/requests/" + configuration["request_id"], {}),
            ("GET", BASE + "/status", {}),
            ("GET", BASE + "/candidates", {}),
            ("POST", BASE + "/bindings", {}),
            ("GET", BASE + "/bindings/requests/" + RECEIPT["request"]["request_id"], {}),
            ("GET", BASE + "/bindings", {"after": "f" * 64}),
        ]
        assert json.loads(calls[1][3]) == configuration
        assert json.loads(calls[5][3]) == RECEIPT["request"]


@pytest.mark.parametrize("outcome", ["lost", 403, 409])
def test_polling_consent_never_reposts_after_ambiguous_or_refused_result(outcome):
    calls = []

    def transport(request):
        calls.append(request)
        if outcome == "lost":
            raise httpx.ReadTimeout("Controlled lost response")
        return httpx.Response(outcome, json={"detail": "not confirmed"})

    with ForgeClient("https://forge.invalid", "fictional-token") as client:
        client.http.close()
        client.http = httpx.Client(base_url="https://forge.invalid/api/v1/", transport=httpx.MockTransport(transport))
        with pytest.raises((httpx.ReadTimeout, RuntimeError)):
            client.configure_scm_polling_binding(PROJECT, REPOSITORY, RECEIPT["request"])
    assert len(calls) == 1 and calls[0].method == "POST"


def test_sdk_rejects_webhook_or_client_supplied_api_proof_before_http():
    calls = []
    with ForgeClient("https://forge.invalid", "fictional-token") as client:
        client.http.close()
        client.http = httpx.Client(
            base_url="https://forge.invalid/api/v1/", transport=httpx.MockTransport(lambda r: calls.append(r))
        )
        with pytest.raises(ValueError):
            client.configure_scm_polling_binding(PROJECT, REPOSITORY, {**RECEIPT["request"], "verified_reference": {}})
    assert not calls
