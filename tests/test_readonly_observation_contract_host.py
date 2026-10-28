# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""An unavailable observation is an explicit decision, never an automatic retry."""

import pytest
from test_original_selection_clients_host import connection

from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1
from firefly_forge.readonly_observation_models import ReadonlyObservationRecoveryInputV1


def body():
    return dict(
        version=1,
        candidate_digest="a" * 64,
        valid_for_seconds=30,
        acknowledge_unavailable_observation=True,
        acknowledge_no_original_callback_replay=True,
        acknowledge_same_request_attempt_and_budgets=True,
        acknowledge_finite_ownership_lease=True,
    )


def test_observation_policy_is_separate_and_defaults_off():
    value = ContinuityLeasePolicyV1(
        version=1, allow_pre_admission_recovery=True, allow_unavailable_source_observations=True
    )
    assert value.allow_readonly_observation_recovery is False
    assert "evidence" not in ReadonlyObservationRecoveryInputV1.model_fields
    assert "acknowledge_reviewed_provider_epoch" not in ReadonlyObservationRecoveryInputV1.model_fields


@pytest.mark.parametrize("field", [k for k in body() if k.startswith("acknowledge_")])
@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_exact_observation_acknowledgements_fail_before_transport(field, value):
    calls = []
    with connection(lambda r: calls.append(r)) as client, pytest.raises(ValueError):
        client.request_readonly_observation_recovery("a" * 32, "b" * 32, "c" * 32, {**body(), field: value})
    assert calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("valid_for_seconds", True),
        ("valid_for_seconds", 0),
        ("valid_for_seconds", 7201),
        ("candidate_digest", "x"),
        ("extra", True),
    ],
)
def test_unknown_or_invalid_fields_fail_before_transport(field, value):
    calls = []
    with connection(lambda r: calls.append(r)) as client, pytest.raises(ValueError):
        client.request_readonly_observation_recovery("a" * 32, "b" * 32, "c" * 32, {**body(), field: value})
    assert calls == []


def test_sdk_uses_same_ticket_and_saved_action_readback():
    import httpx

    calls = []

    def transport(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"status": "queued"})

    with connection(transport) as client:
        assert client.readonly_observation_recovery_candidate("a" * 32, "b" * 32) == {"status": "queued"}
        client.request_readonly_observation_recovery("a" * 32, "b" * 32, "c" * 32, body())
        client.readonly_observation_recovery("a" * 32, "b" * 32)
    assert [method for method, _ in calls] == ["GET", "POST", "GET"]
    assert all("/requests/" + "b" * 32 + "/" in path for _, path in calls)
    assert calls[1][1].endswith("/readonly-observation-recoveries/" + "c" * 32)
