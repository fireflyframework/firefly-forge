# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Recovery SDK submits once under the original ticket and validates acknowledgements."""

import json

import pytest
from test_original_selection_clients_host import connection

from firefly_forge.invalid_read_recovery_models import InvalidReadRecoveryInputV1

RUN, REQUEST, RECOVERY = "a" * 32, "b" * 32, "c" * 32
BODY = dict(
    version=1,
    candidate_digest="1" * 64,
    expected_admission_seal="2" * 64,
    expected_failed_event_digest="3" * 64,
    valid_for_seconds=60,
    acknowledge_never_claimed=True,
    acknowledge_same_request_and_attempt=True,
    acknowledge_preserved_consumption_and_budgets=True,
    acknowledge_renewed_same_attempt_lease=True,
)


def test_original_request_namespace_and_exact_single_submission():
    calls = []
    import httpx

    with connection(lambda r: calls.append(r) or httpx.Response(200, json={"ok": True})) as client:
        client.invalid_read_recovery_candidate(RUN, REQUEST)
        client.request_invalid_read_recovery(RUN, REQUEST, RECOVERY, InvalidReadRecoveryInputV1(**BODY))
        client.invalid_read_recovery(RUN, REQUEST)
    base = f"/api/v1/runs/{RUN}/invalid-read-continuity/requests/{REQUEST}"
    assert [(r.method, r.url.path) for r in calls] == [
        ("GET", base + "/recovery-candidate"),
        ("POST", base + "/recoveries/" + RECOVERY),
        ("GET", base + "/recovery"),
    ]
    assert json.loads(calls[1].content) == BODY


@pytest.mark.parametrize("field", [k for k in BODY if k.startswith("acknowledge_")])
@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_nonliteral_acknowledgements_never_reach_transport(field, value):
    calls = []
    with connection(lambda r: calls.append(r)) as client, pytest.raises(ValueError):
        client.request_invalid_read_recovery(RUN, REQUEST, RECOVERY, {**BODY, field: value})
    assert calls == []
