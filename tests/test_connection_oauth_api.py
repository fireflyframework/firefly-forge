# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from starlette.testclient import TestClient

from firefly_forge.api import create_application

TOKEN = "fixture-factory-operator-32-characters"


def test_native_di_optional_provider_catalog_and_protected_callback(tmp_path, postgres_schema):
    from firefly_forge.connection_oauth import ConnectionOAuth

    with TestClient(create_application(tmp_path, TOKEN), client=("127.0.0.1", 50000)) as client:
        service = client.app.state.runtime.context.get_bean(ConnectionOAuth)
        assert service.store is client.app.state.store
        assert client.get("/api/v1/connection-oauth/providers").status_code == 401
        headers = {"Authorization": "Bearer " + TOKEN}
        response = client.get("/api/v1/connection-oauth/providers", headers=headers)
        assert response.status_code == 200
        assert {item["provider"] for item in response.json()} == {
            "github",
            "figma",
            "sentry",
            "grafana",
            "playwright",
            "github-projects",
            "github-projects-private",
        }
        assert all(item["status"] == "prerequisite_required" for item in response.json())
        assert client.get("/api/v1/connection-oauth/design/callback?code=private&state=private").status_code == 401
        assert (
            client.put(
                "/api/v1/connection-oauth/design",
                headers=headers,
                json={"provider": "figma", "client_id": "client", "public_client": True},
            ).status_code
            == 409
        )
        document = client.get("/api/v1/openapi.json", headers=headers).json()
        operation = document["paths"]["/api/v1/connection-oauth/{name}/callback"]["get"]
        assert operation.get("security", document["security"])


def test_browser_consent_never_accepts_a_service_bearer_as_session(tmp_path, postgres_schema):
    with TestClient(create_application(tmp_path, TOKEN), client=("127.0.0.1", 50000)) as client:
        headers = {"Authorization": "Bearer " + TOKEN}
        response = client.get("/api/v1/connection-oauth/design/continue?ticket=private", headers=headers)
        assert response.status_code == 403
        assert "private" not in response.text
        assert response.headers["referrer-policy"] == "no-referrer"
        response = client.get(
            "/api/v1/connection-oauth/design/continue?ticket=private",
            headers={**headers, "Cookie": "forge_session=forged-cookie"},
        )
        assert response.status_code == 403
        assert "browser" in response.json()["detail"].lower()


async def test_native_authenticated_browser_to_real_http_pkce_callback_refresh_disconnect(
    tmp_path, postgres_schema, monkeypatch
):
    from uuid import uuid4

    import httpx
    from test_connection_oauth_flows import local_provider
    from test_oauth_provider_policy_api import BASE, PROVIDER, registration

    monkeypatch.setenv("FORGE_OAUTH_LOCAL_DEVELOPMENT", "1")
    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", "https://127.0.0.1:49200")
    monkeypatch.setenv("FORGE_ALLOWED_HOSTS", "127.0.0.1")
    monkeypatch.setenv("FORGE_OIDC_ALLOW_LOOPBACK_HTTP", "true")
    async with local_provider(
        callback_uri="https://127.0.0.1:49200/api/v1/connection-oauth/design/callback",
        scope="repository:read",
        resource_path="/mcp/readonly",
        loopback_host="::1",
    ) as (origin, forms, _, _, tokens):
        app = create_application(tmp_path, TOKEN)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 50000)),
                base_url="https://127.0.0.1:49200",
                follow_redirects=False,
            ) as browser,
        ):
            browser.headers["Origin"] = "https://127.0.0.1:49200"
            login = await browser.post("/api/v1/auth/login", json={"token": TOKEN})
            assert login.status_code == 200 and browser.cookies.get("forge_session")
            await browser.get("/api/v1/auth/session")
            browser.headers["X-XSRF-TOKEN"] = browser.cookies["XSRF-TOKEN"]
            policy = await browser.put(BASE + "/" + PROVIDER, json=registration(origin=origin))
            assert policy.status_code == 200, policy.text
            configured = await browser.put(
                "/api/v1/connection-oauth/design",
                json={
                    "provider": PROVIDER,
                    "client_id": "client",
                    "client_secret": "fixture-secret",
                    "authentication": "client_secret_basic",
                },
            )
            assert configured.status_code == 200, configured.text
            handoff = await browser.post("/api/v1/connection-oauth/design/prepare")
            consent = await browser.get(handoff.json()["continuation_url"])
            assert consent.status_code == 303 and consent.headers["referrer-policy"] == "no-referrer"
            assert "HttpOnly" in consent.headers["set-cookie"] and "Secure" in consent.headers["set-cookie"]
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as provider_browser:
                approval = await provider_browser.get(consent.headers["location"])
            callback = approval.headers["location"]
            complete = await browser.get(callback)
            assert complete.status_code == 303 and complete.headers["location"] == "/", complete.text
            metadata = (await browser.get("/api/v1/connection-oauth/design")).json()
            assert metadata["status"] == "authorized_unverified" and not metadata["agent_ready"]
            repeated = await browser.get(callback)
            assert repeated.status_code == 409 and len(forms) == 1
            refreshed = await browser.post("/api/v1/connection-oauth/design/refresh", json={"request_id": uuid4().hex})
            assert refreshed.status_code == 200 and refreshed.json()["grant_revision"] == 2
            assert len(forms) == 2
            disconnect = await browser.post("/api/v1/connection-oauth/design/disconnect")
            assert disconnect.status_code == 200 and disconnect.json()["status"] == "disconnected"
            for secret in ("fixture-secret", tokens[0]["access_token"], tokens[0]["refresh_token"]):
                assert secret not in complete.text and secret not in refreshed.text and secret not in disconnect.text


def test_reserved_name_is_rejected_without_shadowing_the_provider_catalog(tmp_path, postgres_schema, monkeypatch):
    from test_oauth_provider_policy_api import BASE, PROVIDER, registration

    monkeypatch.setenv("FORGE_OAUTH_LOCAL_DEVELOPMENT", "1")
    monkeypatch.setenv("FORGE_OIDC_ALLOW_LOOPBACK_HTTP", "true")
    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", "http://127.0.0.1:49200")

    with TestClient(create_application(tmp_path, TOKEN), client=("127.0.0.1", 50000)) as client:
        headers = {"Authorization": "Bearer " + TOKEN}
        assert client.put(BASE + "/" + PROVIDER, headers=headers, json=registration()).status_code == 200
        body = {"provider": PROVIDER, "client_id": "client", "public_client": True}
        rejected = client.put("/api/v1/connection-oauth/providers", headers=headers, json=body)
        assert rejected.status_code == 409
        catalog = client.get("/api/v1/connection-oauth/providers", headers=headers)
        assert catalog.status_code == 200 and isinstance(catalog.json(), list)
        saved = client.put("/api/v1/connection-oauth/design", headers=headers, json=body)
        assert saved.status_code == 200
        viewed = client.get("/api/v1/connection-oauth/design", headers=headers)
        assert viewed.status_code == 200 and viewed.json()["name"] == "design"
