# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import json

import httpx
import pytest

from firefly_forge.client import ForgeClient


def body():
    return {
        "request_id": "b" * 32,
        "agent": "codex",
        **{
            key: "c" * 64
            for key in (
                "pending_request_digest",
                "source_digest",
                "commands_digest",
                "policy_digest",
                "provider_digest",
            )
        },
    }


def test_sdk_preserves_original_adoption_and_exact_readback_without_retry():
    calls = []

    def reply(request):
        calls.append(request)
        if request.method == "POST":
            assert json.loads(request.content) == body()
            return httpx.Response(503, json={"detail": "Unknown response"})
        return httpx.Response(200, json={"retained": True})

    with ForgeClient("https://forge.example", "owned") as client:
        client.http.close()
        client.http = httpx.Client(base_url="https://forge.example/api/v1/", transport=httpx.MockTransport(reply))
        client.verification_handoff_candidates("a" * 32, "codex")
        with pytest.raises(RuntimeError, match="503"):
            client.adopt_verification_handoff("a" * 32, body())
        client.verification_handoff_adoption("a" * 32, "b" * 32)
    assert [request.method for request in calls] == ["GET", "POST", "GET"]
    assert dict(calls[0].url.params) == {"agent": "codex"}
    assert calls[2].url.path.endswith("/verification-handoffs/adoptions/" + "b" * 32)


@pytest.mark.parametrize(
    "change", [{"request_id": "../new"}, {"source_digest": None}, {"commands": [["arbitrary"]]}, {"agent": "unknown"}]
)
def test_sdk_refuses_untyped_or_arbitrary_command_adoption_before_http(change):
    with ForgeClient("https://forge.example", "owned") as client:
        client.request = lambda *args, **kwargs: pytest.fail("Invalid adoption reached HTTP")
        with pytest.raises(ValueError):
            client.adopt_verification_handoff("a" * 32, {**body(), **change})
