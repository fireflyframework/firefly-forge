# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exact typed source trigger consent; loss never causes an automatic second POST."""

import json

import httpx
import pytest
from test_original_recovery_sdk import connection
from test_scm_source_edit_contract import body

PROJECT, REPOSITORY = "a" * 32, "b" * 32
PATH = f"/api/v1/projects/{PROJECT}/repositories/{REPOSITORY}/scm-polling/source-edit-grants"


def test_typed_source_consent_round_trip_and_original_request_read():
    calls = []
    request = body()

    def transport(req):
        calls.append(req)
        return httpx.Response(201 if req.method == "POST" else 200, json={"request": request})

    with connection(transport) as client:
        assert client.configure_scm_source_edit_grant(PROJECT, REPOSITORY, request) == {"request": request}
        assert client.scm_source_edit_grant(PROJECT, REPOSITORY, request["request_id"]) == {"request": request}
        assert client.current_scm_source_edit_grant(PROJECT, REPOSITORY, request["expected_binding_request_id"]) == {
            "request": request
        }
    assert [(r.method, r.url.path) for r in calls] == [
        ("POST", PATH),
        ("GET", PATH + "/requests/" + request["request_id"]),
        ("GET", PATH + "/current/" + request["expected_binding_request_id"]),
    ]
    assert json.loads(calls[0].content) == request


def test_lost_source_consent_is_never_reposted_or_given_another_identity():
    calls = []
    request = body()

    def transport(req):
        calls.append(req)
        if req.method == "POST":
            raise httpx.ReadTimeout("Public controlled lost result")
        return httpx.Response(404, json={"detail": "Absent original result"})

    with connection(transport) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.configure_scm_source_edit_grant(PROJECT, REPOSITORY, request)
        with pytest.raises(RuntimeError):
            client.scm_source_edit_grant(PROJECT, REPOSITORY, request["request_id"])
    assert [r.method for r in calls] == ["POST", "GET"]


def test_source_consent_invalid_input_is_refused_without_reflection_or_http():
    calls = []
    with connection(lambda req: calls.append(req)) as client:
        with pytest.raises(ValueError) as failure:
            client.configure_scm_source_edit_grant(PROJECT, REPOSITORY, {**body(), "commands": "do-not-reflect"})
        assert "do-not-reflect" not in str(failure.value)
        with pytest.raises(ValueError):
            client.scm_source_edit_grant("../project", REPOSITORY, "a" * 32)
    assert calls == []
