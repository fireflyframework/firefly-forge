# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Typed scoped grant SDK requests never substitute a new identity after delivery."""

import json

import httpx
import pytest

from firefly_forge.client import ForgeClient


def test_scoped_check_sdk_keeps_exact_identities_port_and_current_readback():
    calls = []

    def reply(request):
        calls.append((request.method, request.url.path, dict(request.url.params), request.content))
        return httpx.Response(200, json={"retained": True})

    with ForgeClient("http://fixture.invalid", "public-fixture-token") as client:
        client.http.close()
        client.http = httpx.Client(base_url="http://fixture.invalid/api/v1/", transport=httpx.MockTransport(reply))
        run_id, future_id, grant_id = "a" * 32, "b" * 32, "c" * 32
        assert client.coding_check_candidate(run_id, future_id, bind_port=11447) == {"retained": True}
        body = dict(
            request_id=grant_id,
            recovery_request_id=future_id,
            candidate_digest="d" * 64,
            expected_revision=0,
            command_indices=[0],
            call_limit=2,
            valid_seconds=300,
            bind_port=11447,
            reason="Explicit configured check review.",
        )
        client.grant_coding_checks(run_id, body)
        client.coding_check_receipt(run_id, grant_id)
        client.coding_check_current(run_id)
        client.revoke_coding_checks(
            run_id, dict(request_id="e" * 32, grant_id=grant_id, expected_revision=1, reason="Explicit revocation.")
        )
        assert calls[0][:3] == (
            "GET",
            f"/api/v1/runs/{run_id}/coding-check-grants/candidate",
            {"recovery_request_id": future_id, "bind_port": "11447"},
        )
        assert json.loads(calls[1][3]) == body
        assert [call[0] for call in calls] == ["GET", "POST", "GET", "GET", "POST"]
        assert calls[2][1].endswith("/" + grant_id) and calls[3][1].endswith("/current")
        for changed in (body | {"argv": ["other"]}, body | {"command_indices": [20]}):
            with pytest.raises(ValueError):
                client.grant_coding_checks(run_id, changed)
        with pytest.raises(ValueError):
            client.coding_check_candidate(run_id, future_id, bind_port=1)
        assert len(calls) == 5
