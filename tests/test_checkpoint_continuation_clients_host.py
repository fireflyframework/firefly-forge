# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Same-ticket SDK boundaries validate consent before any transport call."""

import copy
import json

import httpx
import pytest
from test_original_selection_clients_host import connection

RUN, REQUEST, ACTION = "a" * 32, "b" * 32, "c" * 32
BODY = {
    "version": 1,
    "candidate_digest": "1" * 64,
    "valid_for_seconds": 60,
    "acknowledge_retained_response_consumed_once": True,
    "acknowledge_no_completed_effect_replay": True,
    "acknowledge_same_request_attempt_and_budgets": True,
    "acknowledge_finite_ownership_lease": True,
}


@pytest.mark.parametrize("typed", [False, True])
def test_checkpoint_candidate_action_and_status_keep_exact_original_identity(typed):
    from firefly_forge.checkpoint_continuation_models import CheckpointContinuationInputV1

    calls = []
    original = copy.deepcopy(BODY)
    record = {"run_id": RUN, "request_id": REQUEST, "action_id": ACTION, "status": "queued"}
    with connection(lambda r: calls.append(r) or httpx.Response(200, json=record)) as client:
        assert client.checkpoint_continuation_candidate(RUN, REQUEST) == record
        assert (
            client.request_checkpoint_continuation(
                RUN, REQUEST, ACTION, CheckpointContinuationInputV1(**original) if typed else original
            )
            == record
        )
        assert client.checkpoint_continuation_status(RUN, REQUEST) == record
    root = f"/api/v1/runs/{RUN}/checkpoint-continuations/requests/{REQUEST}"
    assert [(r.method, r.url.path) for r in calls] == [
        ("GET", root + "/candidate"),
        ("POST", root + "/actions/" + ACTION),
        ("GET", root + "/status"),
    ]
    assert calls[0].content == calls[2].content == b""
    assert json.loads(calls[1].content) == original == BODY


@pytest.mark.parametrize("field", [name for name in BODY if name.startswith("acknowledge_")])
@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_checkpoint_nonliteral_consent_never_reaches_transport(field, value):
    calls = []
    with connection(lambda r: calls.append(r)) as client, pytest.raises(ValueError):
        client.request_checkpoint_continuation(RUN, REQUEST, ACTION, {**BODY, field: value})
    assert calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"version": 2},
        {"candidate_digest": "x" * 64},
        {"valid_for_seconds": 0},
        {"valid_for_seconds": 7201},
        {"valid_for_seconds": True},
        {"valid_for_seconds": "60"},
        {"new_grant": True},
    ],
)
def test_checkpoint_invalid_body_never_reaches_transport(change):
    calls = []
    with connection(lambda r: calls.append(r)) as client, pytest.raises(ValueError):
        client.request_checkpoint_continuation(RUN, REQUEST, ACTION, {**BODY, **change})
    assert calls == []


@pytest.mark.parametrize("operation", ["candidate", "action", "status"])
@pytest.mark.parametrize("bad", ["../runs", "A" * 32, "a" * 31, "a" * 33])
def test_checkpoint_malformed_identity_never_reaches_transport(operation, bad):
    calls = []
    with connection(lambda r: calls.append(r)) as client, pytest.raises(ValueError):
        if operation == "candidate":
            client.checkpoint_continuation_candidate(bad, REQUEST)
        elif operation == "action":
            client.request_checkpoint_continuation(RUN, REQUEST, bad, BODY)
        else:
            client.checkpoint_continuation_status(RUN, bad)
    assert calls == []
