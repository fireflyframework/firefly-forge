# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import base64
import json
import stat
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture
def app_key():
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        .decode()
    )


def repository_data():
    return {
        "id": 42,
        "full_name": "team/product",
        "clone_url": "https://github.com/team/product.git",
        "default_branch": "main",
    }


async def github_connection(store, app_key, handler):
    from firefly_forge.scm_connections import SCMConnections

    connection = SCMConnections(store, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await connection.configure("github", {"values": {"app_id": "12", "installation_id": "34", "private_key": app_key}})
    return connection


async def test_connection_configuration_is_write_only_owner_private_and_invalidates_tokens(store, app_key):
    from firefly_forge.scm_connections import SCMConnections

    connections = SCMConnections(store)
    result = await connections.configure(
        "github", {"values": {"app_id": "12", "installation_id": "34", "private_key": app_key}}
    )
    assert result["config"]["private_key_configured"] is True
    assert "PRIVATE KEY" not in json.dumps(connections.catalog())
    assert stat.S_IMODE(connections.path.stat().st_mode) == 0o600
    await connections.configure("github", {"clear_secrets": ["private_key"]})
    assert connections.catalog()[0]["status"] == "needs_configuration"
    with pytest.raises(ValueError, match="field"):
        await connections.configure("github", {"values": {"access_token": "manual-secret"}})


async def test_run_pins_application_revision_and_rejects_a_changed_connection_without_freezing_tokens(
    store, app_key, tmp_path
):
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.scm_connections import SCMConnections

    connections = SCMConnections(store)
    store.connections = connections
    await connections.configure("github", {"values": {"app_id": "12", "installation_id": "34", "private_key": app_key}})
    repo = await store.add_repository(
        RepositoryInput(
            name="Product",
            url="https://github.com/team/product.git",
            provider="github",
            connection_provider="github",
            remote_repository_id="42",
        )
    )
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Maintain product"))
    snapshot = run["scm_snapshot"]
    assert snapshot["mode"] == "application" and snapshot["remote_repository_id"] == "42"
    assert "PRIVATE KEY" not in json.dumps(snapshot) and "access_token" not in json.dumps(snapshot)
    connections.assert_snapshot(repo, snapshot)
    await connections.configure("github", {"values": {"installation_id": "35"}})
    with pytest.raises(ValueError, match="application|authorization"):
        connections.assert_snapshot(repo, snapshot)


async def test_redaction_registry_includes_only_known_connection_secret_fields_and_grants(store, app_key):
    from firefly_forge.configuration import redact_secrets
    from firefly_forge.scm_connections import SCMConnections

    connections = SCMConnections(store)
    await connections.configure("github", {"values": {"app_id": "12", "installation_id": "34", "private_key": app_key}})
    await connections.configure(
        "gitlab", {"values": {"client_id": "public-id", "client_secret": "private-client-secret"}}
    )
    data = connections.private._read(connections.path)
    data["gitlab"]["grant"] = {"access_token": "private-access-token", "refresh_token": "private-refresh-token"}
    connections.private._write(connections.path, data)
    connections._tokens["cached-installation"] = ("private-installation-token", 9999999999)
    registered = connections.redaction_settings()
    for secret in [
        app_key,
        "private-client-secret",
        "private-access-token",
        "private-refresh-token",
        "private-installation-token",
    ]:
        assert redact_secrets(secret, registered) == "[redacted]"
    assert "public-id" not in registered.values()
    assert "private-access-token" not in json.dumps(connections.catalog())


async def test_github_app_jwt_and_repo_scoped_credentials_follow_fresh_grants(store, app_key):
    tokens = []
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    def handle(request):
        if request.url.path == "/app/installations/34/access_tokens":
            claim = jwt.decode(request.headers["authorization"].split()[1], options={"verify_signature": False})
            assert claim["iss"] == "12"
            assert 0 < claim["exp"] - claim["iat"] <= 660
            payload = json.loads(request.content)
            tokens.append(payload)
            return httpx.Response(201, json={"token": "scoped-secret", "expires_at": expiry})
        assert request.url.path == "/installation/repositories"
        return httpx.Response(200, json={"repositories": [repository_data()]})

    connections = await github_connection(store, app_key, handle)
    repository = await connections.connect("github", "42")
    assert repository["connection_provider"] == "github"
    token, base = await connections.authentication(repository)
    assert token == "scoped-secret" and base == "https://api.github.com"
    assert tokens[-1] == {"repository_ids": [42], "permissions": {"contents": "write", "pull_requests": "write"}}
    env = await connections.git_environment(repository)
    assert env["FORGE_GIT_USERNAME"] == "x-access-token"
    assert env["FORGE_GIT_PASSWORD"] == "scoped-secret"
    assert "scoped-secret" not in json.dumps(await store.repositories())
    with pytest.raises(ValueError, match="grant|match|origin"):
        await connections.authentication({**repository, "url": "https://attacker.invalid/team/product.git"})


async def test_connection_refuses_cross_origin_pagination_and_redirects(store, app_key):
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    def handle(request):
        if request.method == "POST":
            return httpx.Response(201, json={"token": "token-secret", "expires_at": expiry})
        return httpx.Response(
            200,
            headers={"Link": '<https://attacker.invalid/repos>; rel="next"'},
            json={"repositories": [repository_data()]},
        )

    connections = await github_connection(store, app_key, handle)
    with pytest.raises(ValueError, match="origin|pagination"):
        await connections.repositories("github")


async def test_gitlab_pkce_callback_state_is_bound_single_use_and_refreshes(store):
    from firefly_forge.scm_connections import SCMConnections

    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == "/oauth/token":
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                assert len(form["code_verifier"][0]) >= 43
                return httpx.Response(
                    200, json={"access_token": "initial-secret", "refresh_token": "rotate-secret", "expires_in": 1}
                )
            assert form["refresh_token"] == ["rotate-secret"]
            return httpx.Response(
                200, json={"access_token": "refreshed-secret", "refresh_token": "new-refresh", "expires_in": 7200}
            )
        assert request.headers["authorization"] == "Bearer refreshed-secret"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 7,
                    "path_with_namespace": "team/product",
                    "http_url_to_repo": "https://gitlab.com/team/product.git",
                    "default_branch": "main",
                }
            ],
        )

    connections = SCMConnections(store, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    await connections.configure(
        "gitlab",
        {
            "values": {
                "client_id": "app-client",
                "redirect_uri": "http://127.0.0.1:8787/api/v1/scm/connections/gitlab/callback",
            }
        },
    )
    start = await connections.authorize("gitlab")
    query = parse_qs(urlsplit(start["authorization_url"]).query)
    assert query["code_challenge_method"] == ["S256"]
    state = query["state"][0]
    with pytest.raises(ValueError, match="state|browser"):
        await connections.callback("gitlab", state, "authorization-code", browser_nonce="wrong-cookie")
    result = await connections.callback("gitlab", state, "authorization-code", browser_nonce=start["browser_nonce"])
    assert "initial-secret" not in json.dumps(result)
    with pytest.raises(ValueError, match="state"):
        await connections.callback("gitlab", state, "replay", browser_nonce=start["browser_nonce"])
    assert (await connections.repositories("gitlab"))[0]["id"] == "7"
    assert "refreshed-secret" not in json.dumps(connections.catalog())


async def test_bitbucket_consumer_uses_client_credentials_and_current_bearer_api(store):
    from firefly_forge.scm_connections import SCMConnections

    def handle(request):
        if request.url.host == "bitbucket.org":
            assert request.url.path == "/site/oauth2/access_token"
            assert request.headers["authorization"] == "Basic " + base64.b64encode(b"client-key:client-secret").decode()
            assert parse_qs(request.content.decode())["grant_type"] == ["client_credentials"]
            return httpx.Response(
                200,
                json={
                    "access_token": "consumer-token",
                    "expires_in": 3600,
                    "scope": "repository repository:write pullrequest pullrequest:write",
                },
            )
        assert request.url.host == "api.bitbucket.org"
        assert request.headers["authorization"] == "Bearer consumer-token"
        return httpx.Response(
            200,
            json={
                "values": [
                    {
                        "uuid": "{repo-uuid}",
                        "full_name": "team/product",
                        "mainbranch": {"name": "main"},
                        "links": {"clone": [{"name": "https", "href": "https://bitbucket.org/team/product.git"}]},
                    }
                ]
            },
        )

    connections = SCMConnections(store, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    await connections.configure(
        "bitbucket", {"values": {"client_id": "client-key", "client_secret": "client-secret", "workspace": "team"}}
    )
    repository = await connections.connect("bitbucket", "{repo-uuid}")
    assert (await connections.git_environment(repository))["FORGE_GIT_USERNAME"] == "x-token-auth"
    assert repository["remote_repository_id"] == "{repo-uuid}"


async def test_provider_rejection_never_echoes_credentials_and_cleared_grants_fail_closed(store, app_key):
    connections = await github_connection(store, app_key, lambda request: httpx.Response(401, json={"error": app_key}))
    summary = await connections.test("github")
    assert summary["status"] == "error"
    assert "PRIVATE KEY" not in json.dumps(summary)
    with pytest.raises(ValueError, match="HTTP 401"):
        await connections.repositories("github")


async def test_expired_oauth_state_is_rejected_without_provider_request(store, monkeypatch):
    from firefly_forge.scm_connections import SCMConnections

    def unexpected_request(request):
        raise AssertionError("Expired state must not call the provider")

    connections = SCMConnections(store, client=httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)))
    await connections.configure(
        "gitlab",
        {
            "values": {
                "client_id": "app-client",
                "redirect_uri": "http://127.0.0.1:8787/api/v1/scm/connections/gitlab/callback",
            }
        },
    )
    start = await connections.authorize("gitlab")
    state = parse_qs(urlsplit(start["authorization_url"]).query)["state"][0]
    current = datetime.now(UTC).timestamp()
    monkeypatch.setattr("firefly_forge.scm_connections.time.time", lambda: current + 601)
    with pytest.raises(ValueError, match="expired"):
        await connections.callback("gitlab", state, "unused-code", browser_nonce=start["browser_nonce"])


async def test_revoked_repository_grants_deny_previously_connected_git_access(store, app_key):
    allowed = True
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    def handle(request):
        if request.method == "POST":
            return httpx.Response(201, json={"token": "short-lived-secret", "expires_at": expiry})
        return httpx.Response(200, json={"repositories": [repository_data()] if allowed else []})

    connections = await github_connection(store, app_key, handle)
    repository = await connections.connect("github", "42")
    assert (await connections.git_environment(repository))["FORGE_GIT_PASSWORD"] == "short-lived-secret"
    allowed = False
    with pytest.raises(ValueError, match="current grants"):
        await connections.git_environment(repository)


async def test_bitbucket_missing_application_scopes_fail_closed(store):
    from firefly_forge.scm_connections import SCMConnections

    connections = SCMConnections(
        store,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"access_token": "read-only-secret", "expires_in": 3600, "scope": "repository"}
                )
            )
        ),
    )
    await connections.configure(
        "bitbucket", {"values": {"client_id": "client-key", "client_secret": "client-secret", "workspace": "team"}}
    )
    with pytest.raises(ValueError, match="permissions"):
        await connections.repositories("bitbucket")


async def test_bitbucket_authorization_code_cached_grant_also_requires_write_scopes(store):
    from firefly_forge.scm_connections import SCMConnections

    def handle(request):
        if request.url.path == "/site/oauth2/access_token":
            return httpx.Response(
                200,
                json={
                    "access_token": "read-only-consent",
                    "refresh_token": "refresh-secret",
                    "expires_in": 3600,
                    "scope": "repository",
                },
            )
        return httpx.Response(200, json={"values": []})

    connection = SCMConnections(store, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    await connection.configure(
        "bitbucket",
        {
            "values": {
                "client_id": "client",
                "client_secret": "secret",
                "workspace": "team",
                "grant_type": "authorization_code",
                "redirect_uri": "http://127.0.0.1:8787/api/v1/scm/connections/bitbucket/callback",
            }
        },
    )
    start = await connection.authorize("bitbucket")
    state = parse_qs(urlsplit(start["authorization_url"]).query)["state"][0]
    await connection.callback("bitbucket", state, "code", browser_nonce=start["browser_nonce"])
    with pytest.raises(ValueError, match="permissions"):
        await connection.repositories("bitbucket")


async def test_github_inflight_token_is_rejected_after_application_configuration_changes(store, app_key):
    import asyncio

    started, release = asyncio.Event(), asyncio.Event()
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    async def handle(request):
        started.set()
        await release.wait()
        return httpx.Response(201, json={"token": "obsolete-token", "expires_at": expiry})

    connections = await github_connection(store, app_key, handle)
    pending = asyncio.create_task(connections._github_token("42"))
    await started.wait()
    await connections.configure("github", {"values": {"installation_id": "35"}})
    release.set()
    with pytest.raises(ValueError, match="settings changed"):
        await pending
    assert not connections._tokens


async def test_discovery_rejects_old_grants_after_application_changes(store, app_key):
    import asyncio

    started, release = asyncio.Event(), asyncio.Event()
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    async def handle(request):
        if request.method == "POST":
            return httpx.Response(201, json={"token": "grant-token", "expires_at": expiry})
        started.set()
        await release.wait()
        return httpx.Response(200, json={"repositories": [repository_data()]})

    connections = await github_connection(store, app_key, handle)
    pending = asyncio.create_task(connections.connect("github", "42"))
    await started.wait()
    await connections.configure("github", {"values": {"installation_id": "35"}})
    release.set()
    with pytest.raises(ValueError, match="settings changed"):
        await pending
    assert await store.repositories() == []


@pytest.mark.parametrize("payload", [[], {"token": 12, "expires_at": "2099-01-01T00:00:00Z"}])
async def test_malformed_installation_grants_fail_closed_without_cache(store, app_key, payload):
    connections = await github_connection(store, app_key, lambda request: httpx.Response(201, json=payload))
    with pytest.raises(ValueError, match="invalid installation grant"):
        await connections._github_token()
    assert not connections._tokens


@pytest.mark.parametrize("provider,payload", [("github", []), ("github", {"repositories": [1]}), ("gitlab", {})])
async def test_malformed_repository_pages_fail_with_safe_provider_error(store, app_key, provider, payload):
    from firefly_forge.scm_connections import SCMConnections

    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    def handle(request):
        if request.method == "POST":
            return httpx.Response(201, json={"token": "grant-token", "expires_at": expiry})
        return httpx.Response(200, json=payload)

    connection = SCMConnections(store, client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    if provider == "github":
        await connection.configure(
            provider, {"values": {"app_id": "12", "installation_id": "34", "private_key": app_key}}
        )
    else:
        await connection.configure(
            provider,
            {
                "values": {
                    "client_id": "client",
                    "redirect_uri": "http://127.0.0.1:8787/api/v1/scm/connections/gitlab/callback",
                }
            },
        )
        data = connection.private._read(connection.path)
        data[provider]["grant"] = {"access_token": "grant-token", "expires_at": 9999999999}
        connection.private._write(connection.path, data)
    with pytest.raises(ValueError, match="invalid repository grant"):
        await connection.repositories(provider)


@pytest.mark.parametrize("payload", [[], {"access_token": "token", "expires_in": 3600, "refresh_token": ["secret"]}])
async def test_malformed_oauth_grants_fail_closed_without_persisting(store, payload):
    from firefly_forge.scm_connections import SCMConnections

    connection = SCMConnections(
        store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))),
    )
    await connection.configure(
        "bitbucket", {"values": {"client_id": "client", "client_secret": "secret", "workspace": "team"}}
    )
    with pytest.raises(ValueError, match="invalid OAuth grant"):
        await connection._oauth_token("bitbucket")
    assert "grant" not in connection._record("bitbucket")
