# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Original-selection SDK calls retain the caller's exact admission identity."""

import copy
import json
from contextlib import contextmanager

import httpx
import pytest

from firefly_forge.client import ForgeClient

RUN, REQUEST = "a" * 32, "b" * 32
BODY = {
    "version": 1,
    "candidate_digest": "1" * 64,
    "policy_digest": "2" * 64,
    "operator_observation_digest": "3" * 64,
    "observation_provenance": "native_operator_pre_dispatch_observation",
    "acknowledge_known_local_readiness_failure": True,
    "acknowledge_no_external_model_dispatch": True,
    "acknowledge_original_selection_only": True,
    "acknowledge_original_mode_and_claim_preserved": True,
    "acknowledge_spent_history_and_budgets_preserved": True,
    "acknowledge_same_native_continuation": True,
    "acknowledge_existing_unknown_command_risk": True,
}
RECORD = {"request_id": REQUEST, "run_id": RUN, "digest": "4" * 64}


@contextmanager
def connection(handler):
    with ForgeClient("https://factory.invalid", "fictional-operator-token") as client:
        client.http.close()
        client.http = httpx.Client(
            base_url="https://factory.invalid/api/v1/",
            headers={"Authorization": "Bearer fictional-operator-token"},
            transport=httpx.MockTransport(handler),
        )
        yield client


def test_candidate_admission_and_readback_preserve_caller_body_and_identity():
    calls = []
    original = copy.deepcopy(BODY)

    def handle(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer fictional-operator-token"
        return httpx.Response(201 if request.method == "POST" else 200, json=RECORD)

    with connection(handle) as client:
        assert client.original_selection_candidate(RUN) == RECORD
        assert len(calls) == 1
        assert client.request_original_selection_readmission(RUN, REQUEST, original) == RECORD
        assert client.get_original_selection_readmission(RUN, REQUEST) == RECORD
    prefix = f"/api/v1/runs/{RUN}/original-selection-readmissions"
    assert [(call.method, call.url.path) for call in calls] == [
        ("GET", prefix + "/candidate"),
        ("POST", prefix + "/" + REQUEST),
        ("GET", prefix + "/" + REQUEST),
    ]
    assert calls[0].content == calls[2].content == b""
    assert json.loads(calls[1].content) == BODY == original


@pytest.mark.parametrize("operation", ["candidate", "request", "read"])
@pytest.mark.parametrize("bad_id", ["../runs", "A" * 32, "b" * 31, "b" * 33])
def test_malformed_identifiers_never_reach_transport(operation, bad_id):
    calls = []
    with (
        connection(lambda request: calls.append(request) or httpx.Response(200, json=RECORD)) as client,
        pytest.raises(ValueError),
    ):
        if operation == "candidate":
            client.original_selection_candidate(bad_id)
        elif operation == "request":
            client.request_original_selection_readmission(RUN, bad_id, BODY)
        else:
            client.get_original_selection_readmission(RUN, bad_id)
    assert calls == []


@pytest.mark.parametrize("field", [key for key in BODY if key.startswith("acknowledge_")])
@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_nonliteral_consent_is_rejected_before_transport(field, value):
    calls = []
    with (
        connection(lambda request: calls.append(request) or httpx.Response(200, json=RECORD)) as client,
        pytest.raises(ValueError),
    ):
        client.request_original_selection_readmission(RUN, REQUEST, {**BODY, field: value})
    assert calls == []


@pytest.mark.parametrize("extra", [{"endpoint": "https://other.invalid"}, {"token": "fictional"}, {"attempt": 3}])
def test_connection_and_attempt_overrides_are_rejected_before_transport(extra):
    calls = []
    with (
        connection(lambda request: calls.append(request) or httpx.Response(200, json=RECORD)) as client,
        pytest.raises(ValueError),
    ):
        client.request_original_selection_readmission(RUN, REQUEST, {**BODY, **extra})
    assert calls == []


@pytest.mark.parametrize("operation", ["candidate", "request", "read"])
def test_lost_response_never_retries_or_selects_another_identity(operation):
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("Fictional response lost", request=request)

    with connection(handle) as client, pytest.raises(httpx.ReadTimeout):
        if operation == "candidate":
            client.original_selection_candidate(RUN)
        elif operation == "request":
            client.request_original_selection_readmission(RUN, REQUEST, BODY)
        else:
            client.get_original_selection_readmission(RUN, REQUEST)
    assert len(calls) == 1
    assert calls[0].method == ("POST" if operation == "request" else "GET")
    if operation != "candidate":
        assert calls[0].url.path.endswith("/" + REQUEST)


def test_read_after_lost_admission_is_only_the_original_read_even_when_missing():
    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("Fictional response lost", request=request)
        return httpx.Response(404, json={"detail": "Original admission not found"})

    with connection(handle) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.request_original_selection_readmission(RUN, REQUEST, BODY)
        with pytest.raises(RuntimeError, match="HTTP 404"):
            client.get_original_selection_readmission(RUN, REQUEST)
    assert [call.method for call in calls] == ["POST", "GET"]
    assert calls[0].url.path == calls[1].url.path
