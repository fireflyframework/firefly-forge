# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The SDK reviews source custody and submits one explicit request without retry."""

import json

import httpx
import pytest
from test_git_partial_source_models import consent
from test_original_recovery_sdk import connection

RUN = "a" * 32
BODY = dict(
    version=1,
    request_id="b" * 32,
    expected_head="c" * 40,
    expected_source_digest="e" * 64,
    explicit_partial_source=consent(),
)


def test_partial_source_sdk_uses_exact_typed_paths_and_body():
    calls = []
    with connection(lambda r: calls.append(r) or httpx.Response(200, json={"retained": True})) as client:
        client.partial_source_candidate(RUN)
        client.request_partial_source_checkpoint(RUN, BODY)
    assert [(r.method, r.url.path) for r in calls] == [
        ("GET", f"/api/v1/runs/{RUN}/git-deliveries/partial-source-candidate"),
        ("POST", f"/api/v1/runs/{RUN}/git-deliveries/partial-source-requests"),
    ]
    assert json.loads(calls[1].content) == BODY


def test_unknown_partial_source_sdk_outcome_is_not_automatically_retried():
    calls = []

    def lost(request):
        calls.append(request)
        raise httpx.ReadTimeout("Controlled source-only fixture", request=request)

    with connection(lost) as client, pytest.raises(httpx.ReadTimeout):
        client.request_partial_source_checkpoint(RUN, BODY)
    assert len(calls) == 1
