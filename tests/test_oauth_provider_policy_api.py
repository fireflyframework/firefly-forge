# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from firefly_forge.api import create_application

TOKEN = "fixture-policy-administrator-32-characters"
BASE = "/api/v1/oauth-provider-policies"
PROVIDER = "local-repository-tools"


def registration(*, enabled=True, revision=0, origin="http://[::1]:49152"):
    return {
        "enabled": enabled,
        "expected_revision": revision,
        "request_id": uuid4().hex,
        "local_origin": origin,
    }


def test_native_policy_directory_is_authenticated_and_disabled_by_default(tmp_path, postgres_schema):
    with TestClient(create_application(tmp_path, TOKEN), client=("127.0.0.1", 50000)) as client:
        assert client.get(BASE).status_code == 401
        response = client.get(BASE, headers={"Authorization": "Bearer " + TOKEN})
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.json() == {
            "local_development_available": False,
            "policies": [],
        }
        denied = client.put(BASE + "/" + PROVIDER, headers={"Authorization": "Bearer " + TOKEN}, json=registration())
        assert denied.status_code == 409


def test_supported_local_policy_controls_the_actual_production_di_catalog(tmp_path, postgres_schema, monkeypatch):
    monkeypatch.setenv("FORGE_OAUTH_LOCAL_DEVELOPMENT", "1")
    monkeypatch.setenv("FORGE_OIDC_ALLOW_LOOPBACK_HTTP", "true")
    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", "http://127.0.0.1:49200")
    headers = {"Authorization": "Bearer " + TOKEN}
    with TestClient(create_application(tmp_path, TOKEN), client=("127.0.0.1", 50000)) as client:
        request = registration()
        admitted = client.put(BASE + "/" + PROVIDER, headers=headers, json=request)
        assert admitted.status_code == 200, admitted.text
        packet = admitted.json()
        assert packet["revision"] == 1 and packet["enabled"] is True
        assert packet["provider"]["id"] == PROVIDER
        assert packet["provider"]["resource"] == "http://[::1]:49152/mcp/readonly"
        assert packet["provider"]["scopes"] == ["repository:read"]
        assert client.put(BASE + "/" + PROVIDER, headers=headers, json=request).json() == packet
        stale = client.put(BASE + "/" + PROVIDER, headers=headers, json=registration())
        assert stale.status_code == 409
        catalog = client.get("/api/v1/connection-oauth/providers", headers=headers)
        assert catalog.status_code == 200
        local = next(entry for entry in catalog.json() if entry["provider"] == PROVIDER)
        assert local["status"] == "registration_available" and local["agent_ready"] is False
        configured = client.put(
            "/api/v1/connection-oauth/local-design",
            headers=headers,
            json={"provider": PROVIDER, "client_id": "local-client", "public_client": True},
        )
        assert configured.status_code == 200, configured.text
        assert configured.json()["resource"] == packet["provider"]["resource"]
        disabled = client.put(BASE + "/" + PROVIDER, headers=headers, json=registration(enabled=False, revision=1))
        assert disabled.status_code == 200
        assert client.post("/api/v1/connection-oauth/local-design/prepare", headers=headers).status_code == 409


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:49152",
        "https://example.com:49152",
        "http://127.0.0.1:22",
        "http://127.0.0.1:49152/",
        "http://127.0.0.1:49152?redirect=other",
        "http://127.0.0.1:49152#other",
        "http://user:secret@127.0.0.1:49152",
        "http://127.0.0.1:049152",
        "http://127.0.0.2:49152",
        "http://[::1%zone]:49152",
    ],
)
def test_unsafe_origins_are_rejected_without_policy_state(tmp_path, postgres_schema, monkeypatch, origin):
    monkeypatch.setenv("FORGE_OAUTH_LOCAL_DEVELOPMENT", "1")
    monkeypatch.setenv("FORGE_OIDC_ALLOW_LOOPBACK_HTTP", "true")
    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", "http://127.0.0.1:49200")
    headers = {"Authorization": "Bearer " + TOKEN}
    with TestClient(create_application(tmp_path, TOKEN, role="api"), client=("127.0.0.1", 50000)) as client:
        response = client.put(BASE + "/" + PROVIDER, headers=headers, json=registration(origin=origin))
        assert response.status_code in {400, 422}
        assert client.get(BASE, headers=headers).json()["policies"] == []


def test_new_account_creation_has_an_atomic_revision_precondition(tmp_path, postgres_schema, monkeypatch):
    monkeypatch.setenv("FORGE_OAUTH_LOCAL_DEVELOPMENT", "1")
    monkeypatch.setenv("FORGE_OIDC_ALLOW_LOOPBACK_HTTP", "true")
    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", "http://127.0.0.1:49200")
    headers = {"Authorization": "Bearer " + TOKEN}
    with TestClient(create_application(tmp_path, TOKEN, role="api"), client=("127.0.0.1", 50000)) as client:
        assert client.put(BASE + "/" + PROVIDER, headers=headers, json=registration()).status_code == 200
        body = {"provider": PROVIDER, "client_id": "first-client", "public_client": True, "expected_revision": 0}
        first = client.put("/api/v1/connection-oauth/new-account", headers=headers, json=body)
        assert first.status_code == 200, first.text
        second = client.put(
            "/api/v1/connection-oauth/new-account", headers=headers, json={**body, "client_id": "other-client"}
        )
        assert second.status_code == 409
        current = client.get("/api/v1/connection-oauth/new-account", headers=headers).json()
        assert current["client_id"] == "first-client" and current["revision"] == 1


def test_native_loader_never_advertises_or_uses_mutable_fixture_catalog(tmp_path, postgres_schema):
    from test_connection_oauth_ledger import provider

    from firefly_forge.connection_oauth import ConnectionOAuth

    headers = {"Authorization": "Bearer " + TOKEN}
    with TestClient(create_application(tmp_path, TOKEN), client=("127.0.0.1", 50000)) as client:
        service = client.app.state.runtime.context.get_bean(ConnectionOAuth)
        service.catalog["fixture"] = provider()
        catalog = client.get("/api/v1/connection-oauth/providers", headers=headers)
        assert catalog.status_code == 200
        assert "fixture" not in {item["provider"] for item in catalog.json()}
        assert all(item["status"] == "prerequisite_required" for item in catalog.json())
        denied = client.put(
            "/api/v1/connection-oauth/design",
            headers=headers,
            json={"provider": "fixture", "client_id": "client", "public_client": True},
        )
        assert denied.status_code == 409


@pytest.mark.parametrize("enabled", [False, True])
def test_policy_changes_block_dispatch_but_keep_diagnostics_and_disconnect_usable(
    tmp_path, postgres_schema, monkeypatch, enabled
):
    monkeypatch.setenv("FORGE_OAUTH_LOCAL_DEVELOPMENT", "1")
    monkeypatch.setenv("FORGE_OIDC_ALLOW_LOOPBACK_HTTP", "true")
    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", "http://127.0.0.1:49200")
    headers = {"Authorization": "Bearer " + TOKEN}
    with TestClient(create_application(tmp_path, TOKEN), client=("127.0.0.1", 50000)) as client:
        assert client.put(BASE + "/" + PROVIDER, headers=headers, json=registration()).status_code == 200
        configured = client.put(
            "/api/v1/connection-oauth/design",
            headers=headers,
            json={"provider": PROVIDER, "client_id": "client", "public_client": True},
        )
        assert configured.status_code == 200
        changed = registration(enabled=enabled, revision=1, origin="http://[::1]:49153")
        assert client.put(BASE + "/" + PROVIDER, headers=headers, json=changed).status_code == 200
        directory = client.get("/api/v1/connection-oauth", headers=headers)
        assert directory.status_code == 200, directory.text
        summary = directory.json()[0]
        assert summary["policy_current"] is False
        assert summary["resource"] == "http://[::1]:49152/mcp/readonly"
        assert not summary["allowed_actions"]["authorize"]
        assert not summary["allowed_actions"]["refresh"]
        assert summary["allowed_actions"]["disconnect"]
        assert client.post("/api/v1/connection-oauth/design/prepare", headers=headers).status_code == 409
        disconnected = client.post("/api/v1/connection-oauth/design/disconnect", headers=headers)
        assert disconnected.status_code == 200, disconnected.text
        assert disconnected.json()["status"] == "disconnected"
