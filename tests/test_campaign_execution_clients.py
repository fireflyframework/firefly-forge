# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit execution-window clients perform one typed request, never retry or resume."""

import json

import httpx
import pytest

from firefly_forge.client import ForgeClient


@pytest.mark.parametrize("operation", ["configure", "renew"])
def test_sdk_window_writes_preserve_exact_cas_and_never_retry_unknown_response(operation):
    calls = []

    def transport(request):
        calls.append(request)
        raise httpx.ReadTimeout("Controlled missing execution-window reply")

    with ForgeClient("https://forge.invalid", "controlled-token") as client:
        client.http.close()
        client.http = httpx.Client(base_url="https://forge.invalid/api/v1/", transport=httpx.MockTransport(transport))
        if operation == "configure":
            body = {"request_id": "a" * 32, "expected_previous_digest": None, "policy": {"dispatch_window": 5000}}
            with pytest.raises(httpx.ReadTimeout):
                client.save_campaign_execution_policy("b" * 32, body)
            assert calls[0].method == "PUT"
            assert calls[0].url.path == f"/api/v1/projects/{'b' * 32}/campaign-execution-policy"
            assert json.loads(calls[0].content)["policy"] == {
                "enabled": True,
                "dispatch_window": 5000,
                "max_parallel_runs": 4,
            }
        else:
            body = {
                "request_id": "a" * 32,
                "expected_journal_head": "d" * 64,
                "expected_policy_digest": "e" * 64,
                "additional_dispatches": 2,
                "max_parallel_runs": 1,
            }
            with pytest.raises(httpx.ReadTimeout):
                client.renew_campaign_budget("b" * 32, "c" * 32, body)
            assert calls[0].method == "POST"
            assert calls[0].url.path == f"/api/v1/projects/{'b' * 32}/campaigns/{'c' * 32}/renew-budget"
            assert json.loads(calls[0].content) == body
        assert len(calls) == 1


def test_sdk_rejects_untyped_budget_values_before_dispatch(monkeypatch):
    calls = []
    with ForgeClient("https://forge.invalid", "controlled-token") as client:
        monkeypatch.setattr(client, "request", lambda *args, **kwargs: calls.append(args))
        for invalid in (True, {"request_id": "not-a-request-private-text"}):
            for write in (
                lambda value=invalid: client.save_campaign_execution_policy("b" * 32, value),
                lambda value=invalid: client.renew_campaign_budget("b" * 32, "c" * 32, value),
            ):
                with pytest.raises(ValueError) as failure:
                    write()
                assert "private-text" not in str(failure.value)
    assert calls == []
