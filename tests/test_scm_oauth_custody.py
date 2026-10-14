# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Preserve provider credential custody and uncertain rotating refresh outcomes."""

import json
from urllib.parse import parse_qs

import httpx
import pytest

from firefly_forge.scm_connections import SCMConnections


async def configured(store, provider, client=None):
    connection = SCMConnections(store, client=client)
    values = {"client_id": "fixture-client", "client_secret": "fixture-confidential-secret"}
    if provider == "gitlab":
        values.update(
            host="https://old.example.invalid",
            redirect_uri="https://factory.example.invalid/api/v1/scm/connections/gitlab/callback",
        )
    else:
        values.update(workspace="fixture-team", grant_type="authorization_code")
    await connection.configure(provider, {"values": values})
    return connection


@pytest.mark.parametrize(
    "provider,change",
    [
        ("gitlab", {"host": "https://new.example.invalid"}),
        ("gitlab", {"client_id": "new-client"}),
        ("bitbucket", {"client_id": "new-client"}),
    ],
)
async def test_changed_oauth_destination_requires_explicit_credential_admission(store, provider, change):
    connection = await configured(store, provider)
    original = connection.path.read_bytes()
    with pytest.raises(ValueError, match="credential|secret"):
        await connection.configure(provider, {"values": change})
    assert connection.path.read_bytes() == original


async def test_same_oauth_destination_preserves_write_only_credential(store):
    connection = await configured(store, "gitlab")
    summary = await connection.configure("gitlab", {"values": {"host": "https://old.example.invalid"}})
    assert connection._values("gitlab")["client_secret"] == "fixture-confidential-secret"
    assert "fixture-confidential-secret" not in json.dumps(summary)


async def test_new_destination_accepts_explicit_replacement_or_public_client(store):
    connection = await configured(store, "gitlab")
    await connection.configure(
        "gitlab", {"values": {"host": "https://new.example.invalid", "client_secret": "fixture-new-secret"}}
    )
    assert connection._values("gitlab")["client_secret"] == "fixture-new-secret"
    await connection.configure(
        "gitlab", {"values": {"host": "https://public.example.invalid"}, "clear_secrets": ["client_secret"]}
    )
    assert not connection._values("gitlab")["client_secret"]


def expired_grant(connection, provider):
    data = connection.private._read(connection.path)
    data[provider]["grant"] = {
        "access_token": "fixture-expired-access",
        "refresh_token": "fixture-rotating-refresh",
        "expires_at": 0,
        "scope": "repository:write pullrequest:write",
    }
    connection.private._write(connection.path, data)


@pytest.mark.parametrize("provider", ["gitlab", "bitbucket"])
async def test_lost_refresh_response_is_not_replayed_after_restart(store, provider):
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("fixture lost response", request=request)
        return httpx.Response(
            200,
            json={
                "access_token": "fixture-new-access",
                "refresh_token": "fixture-next-refresh",
                "expires_in": 7200,
                "scope": "repository:write pullrequest:write",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False) as client:
        connection = await configured(store, provider, client)
        expired_grant(connection, provider)
        with pytest.raises(ValueError):
            await connection._oauth_token(provider)
        restarted = SCMConnections(store, client=client)
        with pytest.raises(ValueError, match="uncertain|authoriz"):
            await restarted._oauth_token(provider)
        assert len(requests) == 1
        assert parse_qs(requests[0].content.decode())["refresh_token"] == ["fixture-rotating-refresh"]
        summary = next(item for item in restarted.catalog() if item["provider"] == provider)
        assert summary["status"] == "needs_authorization"
        assert "fixture-rotating-refresh" not in json.dumps(summary)


async def test_interrupted_refresh_intent_blocks_reuse_of_old_access_token(store):
    connection = await configured(store, "gitlab")
    expired_grant(connection, "gitlab")
    data = connection.private._read(connection.path)
    data["gitlab"]["grant"].update(refresh_state="dispatching", expires_at=10**12)
    connection.private._write(connection.path, data)
    with pytest.raises(ValueError, match="uncertain|authoriz"):
        await connection._oauth_token("gitlab")


@pytest.mark.parametrize("marker", [None, False, "", 0])
async def test_malformed_present_refresh_marker_cannot_restore_grant_readiness(store, marker):
    connection = await configured(store, "gitlab")
    expired_grant(connection, "gitlab")
    data = connection.private._read(connection.path)
    data["gitlab"]["grant"].update(refresh_state=marker, expires_at=10**12)
    connection.private._write(connection.path, data)
    with pytest.raises(ValueError, match="uncertain|authoriz"):
        await connection._oauth_token("gitlab")
    summary = next(item for item in connection.catalog() if item["provider"] == "gitlab")
    assert summary["status"] == "needs_authorization"


@pytest.mark.parametrize("provider", ["gitlab", "bitbucket"])
async def test_successful_refresh_replaces_the_intent_and_remains_reusable(store, provider):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "fixture-new-access",
                "refresh_token": "fixture-next-refresh",
                "expires_in": 7200,
                "scope": "repository:write pullrequest:write",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False) as client:
        connection = await configured(store, provider, client)
        expired_grant(connection, provider)
        assert await connection._oauth_token(provider) == "fixture-new-access"
        assert await SCMConnections(store, client=client)._oauth_token(provider) == "fixture-new-access"
        assert len(requests) == 1
        assert not connection._record(provider)["grant"].get("refresh_state")
