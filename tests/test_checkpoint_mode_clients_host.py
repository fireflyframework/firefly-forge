# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Mode adoption keeps caller identities and cannot silently repeat a write."""

import json

import httpx
import pytest
from test_original_recovery_sdk import connection

RUN, REQUEST = "a" * 32, "b" * 32
ACKNOWLEDGEMENTS = (
    "acknowledge_known_pre_dispatch_failure",
    "acknowledge_no_external_model_dispatch",
    "acknowledge_original_intents_not_completed",
    "acknowledge_only_stronger_modes",
    "acknowledge_spent_history_and_budgets_preserved",
    "acknowledge_same_native_continuation",
    "acknowledge_existing_unknown_command_risk",
)


def consent():
    return {
        "version": 1,
        "candidate_digest": "c" * 64,
        "policy_digest": "d" * 64,
        "operator_observation_digest": "e" * 64,
        "observation_provenance": "native_operator_pre_dispatch_observation",
        **{key: True for key in ACKNOWLEDGEMENTS},
    }


def test_candidate_admission_and_readback_keep_exact_wire_identity():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"native": "retained record"})

    with connection(respond) as client:
        assert client.checkpoint_mode_candidate(RUN) == {"native": "retained record"}
        assert client.request_checkpoint_mode_adoption(RUN, REQUEST, consent()) == {"native": "retained record"}
        assert client.get_checkpoint_mode_adoption(RUN, REQUEST) == {"native": "retained record"}
    assert [call.method for call in calls] == ["GET", "POST", "GET"]
    assert calls[0].url.path == f"/api/v1/runs/{RUN}/checkpoint-mode-adoptions/candidate"
    assert calls[1].url.path == calls[2].url.path == f"/api/v1/runs/{RUN}/checkpoint-mode-adoptions/{REQUEST}"
    assert json.loads(calls[1].content) == consent()


@pytest.mark.parametrize("field", ACKNOWLEDGEMENTS)
@pytest.mark.parametrize("value", [False, 1, "true"])
def test_every_operator_acknowledgement_is_explicit_before_http(field, value):
    calls = []
    body = {**consent(), field: value}
    with connection(lambda request: calls.append(request)) as client, pytest.raises(ValueError):
        client.request_checkpoint_mode_adoption(RUN, REQUEST, body)
    assert calls == []


@pytest.mark.parametrize(
    "change",
    [{"exit_code": 0}, {"observation_provenance": "provider_receipt"}, {"operator_observation_digest": "not-a-digest"}],
)
def test_operator_observation_cannot_become_a_provider_result(change):
    calls = []
    with connection(lambda request: calls.append(request)) as client, pytest.raises(ValueError):
        client.request_checkpoint_mode_adoption(RUN, REQUEST, {**consent(), **change})
    assert calls == []


def test_lost_admission_response_requires_same_identity_readback_without_post_retry():
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("Controlled lost response", request=request)
        return httpx.Response(200, json={"native": "same original record"})

    with connection(respond) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.request_checkpoint_mode_adoption(RUN, REQUEST, consent())
        assert len(calls) == 1
        assert client.get_checkpoint_mode_adoption(RUN, REQUEST) == {"native": "same original record"}
    assert [call.method for call in calls] == ["POST", "GET"]
    assert calls[0].url.path == calls[1].url.path


@pytest.mark.parametrize(
    "method,args",
    [
        ("checkpoint_mode_candidate", ("../another-run",)),
        ("request_checkpoint_mode_adoption", (RUN, "../another-request", consent())),
        ("get_checkpoint_mode_adoption", ("../another-run", REQUEST)),
    ],
)
def test_identifiers_cannot_change_the_native_route(method, args):
    calls = []
    with connection(lambda request: calls.append(request)) as client, pytest.raises(ValueError):
        getattr(client, method)(*args)
    assert calls == []
