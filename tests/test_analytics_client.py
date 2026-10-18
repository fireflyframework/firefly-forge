# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Public operator SDK uses only explicit analytics routes and caller configuration."""

import json

import httpx
import pytest

from firefly_forge.client import ForgeClient


def test_analytics_sdk_keeps_exact_project_scope_and_secret_body():
    calls = []

    def reply(request):
        calls.append(request)
        return httpx.Response(200, json={"status": "configured"})

    with ForgeClient("https://forge.example", "owned") as client:
        client.http.close()
        client.http = httpx.Client(base_url="https://forge.example/api/v1/", transport=httpx.MockTransport(reply))
        assert client.analytics_configuration("a" * 32) == {"status": "configured"}
        client.analytics_catalog("a" * 32)
        client.analytics_table("a" * 32, "00000000-0000-0000-0000-000000000004")
        client.analytics_schema_object(
            "a" * 32, "00000000-0000-0000-0000-000000000004", "00000000-0000-0000-0000-000000000005"
        )
    assert [r.method for r in calls] == ["GET"] * 4
    assert [r.url.path for r in calls] == [
        "/api/v1/projects/" + "a" * 32 + "/analytics/configuration",
        "/api/v1/projects/" + "a" * 32 + "/analytics/catalog",
        "/api/v1/projects/" + "a" * 32 + "/analytics/tables/00000000-0000-0000-0000-000000000004",
        "/api/v1/projects/"
        + "a" * 32
        + "/analytics/tables/00000000-0000-0000-0000-000000000004/objects/00000000-0000-0000-0000-000000000005",
    ]


def test_analytics_sdk_denies_invalid_scope_before_http():
    with ForgeClient("https://forge.example", "owned") as client:
        client.request = lambda *a, **k: pytest.fail("invalid ID reached HTTP")
        with pytest.raises(ValueError):
            client.analytics_table("a" * 32, "../other")


def test_configuration_sdk_transmits_explicit_secret_once_without_masking_or_retry():
    body = dict(
        expected_revision=0,
        enabled=True,
        endpoint="https://analytics.example",
        tenant_id="fictional",
        workspace_id="00000000-0000-0000-0000-000000000002",
        dataset_id="00000000-0000-0000-0000-000000000003",
        agent_token="owned-scoped-token",
    )
    calls = []

    def reply(request):
        calls.append(request)
        assert request.method == "PUT" and json.loads(request.content) == body
        return httpx.Response(404, json={"detail": "absent"})

    with ForgeClient("https://forge.example", "owned") as client:
        client.http.close()
        client.http = httpx.Client(base_url="https://forge.example/api/v1/", transport=httpx.MockTransport(reply))
        with pytest.raises(RuntimeError, match="404"):
            client.configure_analytics("a" * 32, body)
    assert len(calls) == 1
