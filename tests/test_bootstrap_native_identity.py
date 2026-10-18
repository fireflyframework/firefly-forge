# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native bootstrap bearer authority has a durable, revocable identity fence."""

import asyncio
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from starlette.testclient import TestClient
from test_identity import request

from firefly_forge.api import create_application
from firefly_forge.identity import IdentityConfiguration, IdentityService, Principal
from firefly_forge.identity_persistence import ForgeIdentitySessionEntity, ForgeIdentityUserEntity
from firefly_forge.project_access import AccessDenied, ProjectAccess

BOOTSTRAP_ID = UUID("eb6e9e6f-4311-5435-a84f-e35ae14c19af")
TOKEN = "owned-bootstrap-native-identity-fixture"


def service(store):
    return IdentityService(store, config=IdentityConfiguration(mode="local_bootstrap", bootstrap_token=TOKEN))


@pytest_asyncio.fixture
async def bootstrap(store):
    instance = service(store)
    try:
        yield instance
    finally:
        await instance.client.aclose()


async def counts(store):
    async with store._unit() as unit:
        return (
            await unit.scalar(select(func.count()).select_from(ForgeIdentityUserEntity)),
            await unit.scalar(select(func.count()).select_from(ForgeIdentitySessionEntity)),
        )


async def test_native_bootstrap_bearer_materializes_canonical_user_without_session(store, bootstrap):
    actor = await bootstrap.authenticate(request(bearer="Bearer " + TOKEN))
    assert actor is not None
    assert (actor.id, actor.issuer, actor.subject, actor.authentication_method, actor.kind) == (
        BOOTSTRAP_ID,
        "local:bootstrap",
        "operator",
        "local_bootstrap",
        "local_bootstrap",
    )
    async with store._unit() as unit:
        user = await unit.get(ForgeIdentityUserEntity, BOOTSTRAP_ID)
        assert user is not None and user.enabled
        assert (user.issuer, user.subject) == ("local:bootstrap", "operator")
    assert await bootstrap.authenticate(request(bearer="Bearer " + TOKEN)) == actor
    assert await counts(store) == (1, 0)


async def test_native_concurrent_bootstrap_first_bearers_share_one_identity(store, store_factory, tmp_path):
    other_store = await store_factory(tmp_path / "second" / "factory.db")
    first, second = service(store), service(other_store)
    try:
        actors = await asyncio.gather(
            first.authenticate(request(bearer="Bearer " + TOKEN)),
            second.authenticate(request(bearer="Bearer " + TOKEN)),
        )
        assert actors[0] is not None and actors[0] == actors[1]
        assert actors[0].id == BOOTSTRAP_ID
        assert await counts(store) == (1, 0)
    finally:
        await first.client.aclose()
        await second.client.aclose()


@pytest.mark.parametrize("method", ["bearer", "login"])
@pytest.mark.parametrize("change", ["disabled", "wrong_id", "wrong_issuer", "wrong_subject"])
async def test_native_bootstrap_refuses_existing_disabled_or_mismatched_identity(store, bootstrap, method, change):
    expected = {
        "id": uuid4() if change == "wrong_id" else BOOTSTRAP_ID,
        "issuer": "another:issuer" if change == "wrong_issuer" else "local:bootstrap",
        "subject": "another-subject" if change == "wrong_subject" else "operator",
        "enabled": change != "disabled",
    }
    async with store._unit() as unit:
        unit.add(ForgeIdentityUserEntity(**expected))
    if method == "bearer":
        assert await bootstrap.authenticate(request(bearer="Bearer " + TOKEN)) is None
    else:
        with pytest.raises(ValueError, match="identity"):
            await bootstrap.login_bootstrap(TOKEN, request())
    async with store._unit() as unit:
        user = await unit.scalar(select(ForgeIdentityUserEntity))
        assert {key: getattr(user, key) for key in expected} == expected
    assert await counts(store) == (1, 0)


@pytest.mark.parametrize("change", ["remote", "wrong_token", "oversized"])
async def test_native_invalid_bootstrap_bearer_never_materializes_identity(store, bootstrap, change):
    value = "Bearer " + ("incorrect" if change == "wrong_token" else TOKEN)
    if change == "oversized":
        value = "Bearer " + "x" * 16385
    incoming = request(bearer=value)
    if change == "remote":
        incoming.scope["client"] = ("203.0.113.10", 1234)
    assert await bootstrap.authenticate(incoming) is None
    assert await counts(store) == (0, 0)


async def test_native_unknown_oidc_actor_does_not_gain_bootstrap_materialization(store):
    actor = Principal(
        uuid4(),
        "https://unknown.example/realm",
        "unregistered",
        api_roles=frozenset({"forge-admin"}),
        api_audience="forge-api",
    )
    with pytest.raises(AccessDenied, match="unavailable"):
        await ProjectAccess(store).authorize_actor(ProjectAccess.actor_snapshot(actor), None, "execute")
    assert await counts(store) == (0, 0)


def test_native_bootstrap_bearer_admits_legacy_and_original_run_without_browser_login(tmp_path, postgres_schema):
    with TestClient(
        create_application(tmp_path / "api", TOKEN, role="api"),
        headers={"Authorization": "Bearer " + TOKEN},
        client=("127.0.0.1", 52763),
    ) as client:
        repo = client.post("/api/v1/repositories", json={"name": "Owned admission", "url": str(tmp_path)})
        assert repo.status_code == 201
        body = {
            "repository_id": repo.json()["id"],
            "brief": "Verify canonical bootstrap admission",
            "operation": "test",
        }
        legacy = client.post("/api/v1/runs", json=body)
        assert legacy.status_code == 201, legacy.text
        url = "/api/v1/run-creation-requests/" + "c" * 32
        original = client.post(url, json={"version": 1, "input": body})
        assert original.status_code == 201, original.text
        assert client.get(url).content == original.content
        repeated = client.post(url, json={"version": 1, "input": body})
        assert repeated.status_code == 200 and repeated.content == original.content
        assert len(client.get("/api/v1/runs").json()) == 2
        assert "set-cookie" not in legacy.headers and "set-cookie" not in original.headers
