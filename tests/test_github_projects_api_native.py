# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Full native DI, browser consent, typed REST and SDK with controlled GitHub HTTP."""

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
from pyfly.oauth2 import OAuth2Client, OAuth2Endpoints
from starlette.testclient import TestClient
from test_bootstrap_native_identity import TOKEN
from test_github_projects_native import create_body, creation_server
from test_projects import definition

from firefly_forge.api import create_application
from firefly_forge.client import ForgeClient
from firefly_forge.connection_oauth import ConnectionOAuth
from firefly_forge.github_projects import GitHubProjects


def test_native_api_browser_consent_private_creation_sdk_readback_and_logout(tmp_path, postgres_schema, monkeypatch):
    origin = "https://127.0.0.1:49200"
    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", origin)
    monkeypatch.setenv("FORGE_ALLOWED_HOSTS", "127.0.0.1")
    app = create_application(tmp_path, TOKEN, role="api")
    with TestClient(app, base_url=origin, client=("127.0.0.1", 50000), follow_redirects=False) as browser:
        assert browser.get("/api/v1/connection-oauth/providers").status_code == 401
        browser.headers["Origin"] = origin
        missing_csrf = browser.post("/api/v1/auth/login", json={"token": TOKEN})
        assert missing_csrf.status_code == 403 and missing_csrf.json()["error"] == "CSRF token missing"
        browser.headers["X-XSRF-TOKEN"] = browser.cookies["XSRF-TOKEN"]
        login = browser.post("/api/v1/auth/login", json={"token": TOKEN})
        assert login.status_code == 200, login.text
        assert browser.get("/api/v1/auth/session").status_code == 200
        browser.headers["X-XSRF-TOKEN"] = browser.cookies["XSRF-TOKEN"]
        repository = browser.post(
            "/api/v1/repositories",
            json={
                "name": "Private board fixture",
                "url": "https://github.com/team/product",
                "provider": "github",
                "remote_repository_id": "77",
            },
        )
        assert repository.status_code == 201, repository.text
        repo_id = repository.json()["id"]
        project = browser.post("/api/v1/projects", json=definition(repo_id).model_dump(mode="json"))
        assert project.status_code == 201, project.text
        project_id = project.json()["id"]
        oauth = app.state.runtime.context.get_bean(ConnectionOAuth)
        assert (
            browser.put(
                "/api/v1/connection-oauth/github-board",
                json={
                    "provider": "github-projects-private",
                    "project_id": project_id,
                    "client_id": "synthetic-client",
                    "client_secret": "synthetic-oauth-secret",
                },
            ).status_code
            == 200
        )

        def token_reply(request):
            form = parse_qs(request.content.decode())
            assert form["code_verifier"] and "resource" not in form
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-github-access-token",
                    "token_type": "bearer",
                    "scope": "project,repo",
                },
            )

        monkeypatch.setattr(
            oauth,
            "_client",
            lambda frozen, secret: OAuth2Client(
                frozen["client_id"],
                OAuth2Endpoints(frozen["policy"]["token_endpoint"]),
                client_secret=secret,
                transport=httpx.MockTransport(token_reply),
            ),
        )
        handoff = browser.post("/api/v1/connection-oauth/github-board/prepare")
        assert handoff.status_code == 200
        consent = browser.get(handoff.json()["continuation_url"])
        assert consent.status_code == 303
        parameters = parse_qs(urlsplit(consent.headers["location"]).query)
        assert parameters["code_challenge_method"] == ["S256"]
        callback = browser.get(
            "/api/v1/connection-oauth/github-board/callback",
            params={
                "state": parameters["state"][0],
                "code": "controlled-code",
            },
        )
        assert callback.status_code == 303
        summary = browser.get("/api/v1/connection-oauth/github-board").json()
        assert summary["grant_expires_at"] is None and summary["refresh_available"] is False

        manager = app.state.runtime.context.get_bean(GitHubProjects)
        controlled = SimpleNamespace(state={"handle": None})
        state = creation_server(controlled)

        def github_reply(request):
            assert request.headers["authorization"] == "Bearer synthetic-github-access-token"
            return controlled.state["handle"](request)

        manager.remote.inner = httpx2.MockTransport(github_reply)
        body = create_body()
        base = f"/api/v1/projects/{project_id}/repositories/{repo_id}/github-projects"
        created = browser.post(base + "/creations", json=body.model_dump(mode="json"))
        assert created.status_code == 200, created.text
        assert created.json()["status"] == "completed" and created.json()["board"]["private"] is True
        assert created.headers["cache-control"] == "no-store" and state["creates"] == 1
        assert "synthetic-github-access-token" not in created.text

        with ForgeClient(origin, TOKEN) as sdk:
            sdk.http.close()
            sdk.http = browser
            browser.base_url = origin + "/api/v1/"
            receipt = sdk.github_projects_creation(project_id, repo_id, body.request_id)
            assert receipt.model_dump(mode="json") == created.json()
            replay = sdk.create_github_projects(project_id, repo_id, body)
            assert replay == receipt and state["creates"] == 1
            invalid = browser.post(
                base + "/creations", json={**body.model_dump(mode="json"), "access_token": "injected"}
            )
            assert invalid.status_code >= 400 and state["creates"] == 1
            assert browser.post("/api/v1/auth/logout").status_code == 200
            assert browser.get(base + "/creations/" + body.request_id).status_code == 401
