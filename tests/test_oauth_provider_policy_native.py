# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual native API replicas share immutable, administrator-admitted policy state."""

import asyncio
import os
from contextlib import asynccontextmanager
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from starlette.testclient import TestClient
from test_oauth_provider_policy_api import BASE, PROVIDER, TOKEN, registration

from firefly_forge.api import create_application
from firefly_forge.connection_oauth import ConnectionOAuth
from firefly_forge.oauth_provider_policies import OAuthProviderPolicies
from firefly_forge.oauth_provider_policy_persistence import OAuthProviderPolicyRepository


def environment(monkeypatch):
    monkeypatch.setenv("FORGE_OAUTH_LOCAL_DEVELOPMENT", "1")
    monkeypatch.setenv("FORGE_OIDC_ALLOW_LOOPBACK_HTTP", "true")
    monkeypatch.setenv("FORGE_BROWSER_ORIGIN", "https://127.0.0.1:49200")
    monkeypatch.setenv("FORGE_ALLOWED_HOSTS", "127.0.0.1")


@asynccontextmanager
async def native_replicas(tmp_path, monkeypatch):
    environment(monkeypatch)
    apps = [create_application(tmp_path / "shared", TOKEN, role="api") for _ in range(2)]
    async with (
        apps[0].router.lifespan_context(apps[0]),
        apps[1].router.lifespan_context(apps[1]),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=apps[0], client=("127.0.0.1", 50000)),
            base_url="https://127.0.0.1:49200",
            headers={"Authorization": "Bearer " + TOKEN},
        ) as first,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=apps[1], client=("127.0.0.1", 50000)),
            base_url="https://127.0.0.1:49200",
            headers={"Authorization": "Bearer " + TOKEN},
        ) as second,
    ):
        yield apps, first, second


async def test_two_native_replicas_admit_one_cas_winner_and_keep_exact_operation_receipt(
    tmp_path, postgres_schema, monkeypatch
):
    async with native_replicas(tmp_path, monkeypatch) as (_, first, second):
        requests = [registration(), registration(origin="http://[::1]:49153")]
        results = await asyncio.gather(
            *[
                client.put(BASE + "/" + PROVIDER, json=request)
                for client, request in zip((first, second), requests, strict=True)
            ]
        )
        assert sorted(result.status_code for result in results) == [200, 409]
        winner = next(i for i, result in enumerate(results) if result.status_code == 200)
        current = (await second.get(BASE)).json()["policies"][0]
        assert current == results[winner].json()
        replay = await first.put(BASE + "/" + PROVIDER, json=requests[winner])
        assert replay.status_code == 200 and replay.json() == current
        changed_body = {**requests[winner], "enabled": False}
        assert (await second.put(BASE + "/" + PROVIDER, json=changed_body)).status_code == 409
        assert (await first.get(BASE)).json()["policies"][0] == current


async def test_native_di_uses_one_store_session_provider_and_policy_repository(tmp_path, postgres_schema, monkeypatch):
    async with native_replicas(tmp_path, monkeypatch) as (apps, first, _):
        app = apps[0]
        context = app.state.runtime.context
        policies = context.get_bean(OAuthProviderPolicies)
        connections = context.get_bean(ConnectionOAuth)
        assert policies.store is app.state.store is connections.store
        assert policies.records is context.get_bean(OAuthProviderPolicyRepository)
        assert connections.policy_lookup.__self__ is policies
        assert connections.policy_directory.__self__ is policies
        assert (await first.put(BASE + "/" + PROVIDER, json=registration())).status_code == 200
        async with policies.store._unit() as session:
            binding = await policies.lookup(session, PROVIDER)
            assert binding.revision == 1 and binding.provider.scopes == ("repository:read",)


async def test_native_second_replica_observes_policy_withdrawal_without_restart(tmp_path, postgres_schema, monkeypatch):
    async with native_replicas(tmp_path, monkeypatch) as (_, first, second):
        assert (await first.put(BASE + "/" + PROVIDER, json=registration())).status_code == 200
        account = await second.put(
            "/api/v1/connection-oauth/design",
            json={"provider": PROVIDER, "client_id": "client", "public_client": True},
        )
        assert account.status_code == 200
        assert (await second.post("/api/v1/connection-oauth/design/prepare")).status_code == 200
        assert (await first.put(BASE + "/" + PROVIDER, json=registration(enabled=False, revision=1))).status_code == 200
        assert (await second.post("/api/v1/connection-oauth/design/prepare")).status_code == 409
        observed = await second.get("/api/v1/connection-oauth/design")
        assert observed.status_code == 200 and observed.json()["policy_current"] is False
        assert (await second.post("/api/v1/connection-oauth/design/disconnect")).status_code == 200


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE forge_oauth_provider_policy_revisions SET digest = 'changed'",
        "DELETE FROM forge_oauth_provider_policy_revisions",
        "UPDATE forge_oauth_provider_policy_operations SET request_id = 'changed'",
        "DELETE FROM forge_oauth_provider_policy_operations",
        "UPDATE forge_oauth_provider_policies SET revision = revision",
        "DELETE FROM forge_oauth_provider_policies",
    ],
)
async def test_database_refuses_rewriting_or_erasing_policy_admission_history(
    tmp_path, postgres_schema, monkeypatch, statement
):
    async with native_replicas(tmp_path, monkeypatch) as (apps, first, second):
        admitted = await first.put(BASE + "/" + PROVIDER, json=registration())
        assert admitted.status_code == 200
        with pytest.raises(DBAPIError, match="immutable|revisions must advance"):
            async with apps[0].state.store._unit() as session:
                await session.execute(text(statement))
        assert (await second.get(BASE)).json()["policies"][0] == admitted.json()


async def test_current_native_disabled_administrator_cannot_write_or_read_policies(
    tmp_path, postgres_schema, monkeypatch
):
    async with native_replicas(tmp_path, monkeypatch) as (apps, first, second):
        login = await first.post(
            "/api/v1/auth/login", json={"token": TOKEN}, headers={"Origin": "https://127.0.0.1:49200"}
        )
        assert login.status_code == 200
        admitted = await first.put(BASE + "/" + PROVIDER, json=registration())
        assert admitted.status_code == 200
        async with apps[0].state.store._unit() as session:
            result = await session.execute(
                text("UPDATE forge_identity_users SET enabled = false WHERE id = :id"),
                {"id": uuid5(NAMESPACE_URL, "firefly-forge:local-bootstrap")},
            )
            assert result.rowcount == 1
        assert (await second.get(BASE)).status_code in {401, 403}
        assert (await second.put(BASE + "/" + PROVIDER, json=registration(revision=1))).status_code in {401, 403}
        async with apps[0].state.store._unit() as session:
            assert await session.scalar(text("SELECT count(*) FROM forge_oauth_provider_policy_revisions")) == 1


@pytest.mark.parametrize("missing", ["local_flag", "loopback_flag", "literal_origin"])
def test_all_three_development_gates_are_required_before_admission(tmp_path, postgres_schema, monkeypatch, missing):
    environment(monkeypatch)
    if missing == "local_flag":
        monkeypatch.delenv("FORGE_OAUTH_LOCAL_DEVELOPMENT")
    elif missing == "loopback_flag":
        monkeypatch.delenv("FORGE_OIDC_ALLOW_LOOPBACK_HTTP")
    else:
        monkeypatch.setenv("FORGE_BROWSER_ORIGIN", "https://forge.example")
        monkeypatch.setenv("FORGE_ALLOWED_HOSTS", "forge.example")
    with TestClient(
        create_application(tmp_path, TOKEN), base_url=os.environ["FORGE_BROWSER_ORIGIN"], client=("127.0.0.1", 50000)
    ) as client:
        headers = {"Authorization": "Bearer " + TOKEN}
        assert client.get(BASE, headers=headers).json()["local_development_available"] is False
        denied = client.put(BASE + "/" + PROVIDER, headers=headers, json=registration())
        assert denied.status_code == 409
        assert client.get(BASE, headers=headers).json()["policies"] == []
