# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Final-allowance SDK preserves caller identity and never retries uncertain writes."""

import json

import httpx
import pytest
from test_original_recovery_sdk import connection

RUN, REQUEST = "a" * 32, "b" * 32
BODY = dict(
    request_id=REQUEST,
    candidate_digest="c" * 64,
    native_process_digest="d" * 64,
    agent="codex",
    reason="Reviewed controlled mitigation",
    mitigation_evidence_digest="e" * 64,
    adopt_current_executable=True,
)


def test_sdk_reserves_exact_identity_then_reads_original_without_automatic_dispatch():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.content))
        return httpx.Response(201 if request.method == "POST" else 200, json={"retained": BODY})

    with connection(handler) as client:
        assert client.reserve_provider_recovery_allowance(RUN, BODY) == {"retained": BODY}
        assert client.provider_recovery_allowance(RUN, REQUEST) == {"retained": BODY}
    assert [(m, p) for m, p, _ in calls] == [
        ("POST", f"/api/v1/runs/{RUN}/provider-recoveries/allowances"),
        ("GET", f"/api/v1/runs/{RUN}/provider-recoveries/allowances/{REQUEST}"),
    ]
    assert json.loads(calls[0][2]) == BODY and calls[1][2] == b""


def test_lost_response_is_one_write_and_invalid_budget_never_reaches_transport():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("Controlled uncertain response")

    with connection(handler) as client:
        with pytest.raises(ValueError):
            client.reserve_provider_recovery_allowance(RUN, BODY | {"next_attempt": 6})
        assert calls == []
        with pytest.raises(httpx.ReadTimeout):
            client.reserve_provider_recovery_allowance(RUN, BODY)
    assert len(calls) == 1 and json.loads(calls[0].content) == BODY
