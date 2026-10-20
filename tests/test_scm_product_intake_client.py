# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Public intake self-consent and original receipt reads never dispatch a run."""

import json

import httpx
import pytest

from firefly_forge.client import ForgeClient


def consent():
    return dict(
        version=1,
        request_id="1" * 32,
        expected_previous_digest=None,
        verified_receipt_id="2" * 32,
        expected_verified_reference_digest="3" * 64,
        active=True,
        acknowledge_product_intake=True,
    )


def test_sdk_keeps_the_original_consent_body_and_exact_read_only_request_identity():
    calls = []

    def remote(request):
        calls.append(request)
        return httpx.Response(200, json={"digest": "4" * 64})

    with ForgeClient("https://forge.example", "owned") as client:
        client.http.close()
        client.http = httpx.Client(base_url="https://forge.example/api/v1/", transport=httpx.MockTransport(remote))
        client.configure_scm_product_intake_consent("a" * 32, consent())
        client.scm_product_intake_consent("a" * 32, "1" * 32)
        client.scm_product_intake("a" * 32, "5" * 32)
    assert [(r.method, r.url.path) for r in calls] == [
        ("POST", "/api/v1/repositories/" + "a" * 32 + "/scm-product-intake/consents"),
        ("GET", "/api/v1/repositories/" + "a" * 32 + "/scm-product-intake/consents/" + "1" * 32),
        ("GET", "/api/v1/repositories/" + "a" * 32 + "/scm-product-intake/requests/" + "5" * 32),
    ]
    assert json.loads(calls[0].content) == consent()


@pytest.mark.parametrize(
    "update", [{"native_user_id": "a" * 32}, {"active": "true"}, {"acknowledge_product_intake": 1}, {"version": True}]
)
def test_invalid_self_consent_never_reaches_http(update):
    with ForgeClient("https://forge.example", "owned") as client:
        client.request = lambda *args, **kwargs: pytest.fail("Invalid authority reached HTTP")
        with pytest.raises(ValueError):
            client.configure_scm_product_intake_consent("a" * 32, {**consent(), **update})
