# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Provider ports cannot isolate the factory's host-only browser cookies."""

import pytest
from starlette.testclient import TestClient
from test_oauth_provider_policy_api import BASE, PROVIDER, TOKEN, registration

from firefly_forge.api import create_application


@pytest.mark.parametrize(
    "factory,provider",
    [
        ("http://127.0.0.1:49200", "http://127.0.0.1:49152"),
        ("http://[::1]:49200", "http://[::1]:49152"),
        ("https://127.0.0.1:49200", "http://127.0.0.1:49152"),
    ],
)
def test_same_host_policy_is_rejected_before_retaining_browser_consent_configuration(
    tmp_path, postgres_schema, monkeypatch, factory, provider
):
    monkeypatch.setenv("FORGE_OAUTH_LOCAL_DEVELOPMENT", "1")
    monkeypatch.setenv("FORGE_OIDC_ALLOW_LOOPBACK_HTTP", "true")
    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", factory)
    headers = {"Authorization": "Bearer " + TOKEN}
    with TestClient(create_application(tmp_path, TOKEN), client=("127.0.0.1", 50000)) as client:
        denied = client.put(BASE + "/" + PROVIDER, headers=headers, json=registration(origin=provider))
        assert denied.status_code == 409, denied.text
        assert "different" in denied.json()["detail"].lower()
        assert client.get(BASE, headers=headers).json()["policies"] == []


def test_factory_host_change_blocks_retained_unsafe_policy_without_erasing_diagnostics(
    tmp_path, postgres_schema, monkeypatch
):
    monkeypatch.setenv("FORGE_OAUTH_LOCAL_DEVELOPMENT", "1")
    monkeypatch.setenv("FORGE_OIDC_ALLOW_LOOPBACK_HTTP", "true")
    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", "http://127.0.0.1:49200")
    headers = {"Authorization": "Bearer " + TOKEN}
    origin = "http://[::1]:49152"
    with TestClient(create_application(tmp_path, TOKEN), client=("127.0.0.1", 50000)) as client:
        admitted = client.put(BASE + "/" + PROVIDER, headers=headers, json=registration(origin=origin))
        assert admitted.status_code == 200
        account = client.put(
            "/api/v1/connection-oauth/design",
            headers=headers,
            json={"provider": PROVIDER, "client_id": "client", "public_client": True},
        )
        assert account.status_code == 200
    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", "http://[::1]:49200")
    with TestClient(create_application(tmp_path, TOKEN), client=("127.0.0.1", 50000)) as replica:
        retained = replica.get(BASE, headers=headers)
        assert retained.status_code == 200 and retained.json()["policies"] == [admitted.json()]
        catalog = replica.get("/api/v1/connection-oauth/providers", headers=headers).json()
        entry = next(row for row in catalog if row["provider"] == PROVIDER)
        assert entry["status"] == "prerequisite_required"
        summary = replica.get("/api/v1/connection-oauth/design", headers=headers)
        assert summary.status_code == 200 and summary.json()["policy_current"] is False
        assert replica.post("/api/v1/connection-oauth/design/prepare", headers=headers).status_code == 409
        assert replica.post("/api/v1/connection-oauth/design/disconnect", headers=headers).status_code == 200
