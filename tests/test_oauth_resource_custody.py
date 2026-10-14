# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native resource bindings never export tokens or widen captured authority."""

import json
from uuid import uuid4

import pytest
from test_connection_oauth_flows import complete, local_provider, setup
from test_projects import definition

from firefly_forge.connection_oauth_models import OAuthConnectionInput
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.projects import Projects


async def admitted(store, tmp_path, origin):
    from firefly_forge.oauth_resources import OAuthResources

    connection, user, _, _, _, _ = await setup(store, origin)
    repo = await store.add_repository(RepositoryInput(name="Owned source", url="https://github.com/team/product.git"))
    project = await Projects(store).configure(definition(repo["id"]))
    await connection.configure(
        "design",
        OAuthConnectionInput(
            provider="fixture", client_id="client", client_secret="fixture-secret", project_id=project["id"]
        ),
        user,
    )
    handoff = await connection.prepare("design", user)
    challenge = await connection.begin("design", handoff["ticket"], user, browser_authenticated=True)
    from urllib.parse import parse_qs, urlsplit

    import httpx

    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
        response = await client.get(challenge.authorization_url)
    callback = parse_qs(urlsplit(response.headers["location"]).query)
    await complete(connection, user, challenge, callback)
    context = CURRENT_PRINCIPAL.set(user)
    try:
        run = await store.create_run(
            RunInput(
                repository_id=repo["id"],
                project_id=project["id"],
                component_id="stock",
                brief="Review the captured repository",
            )
        )
    finally:
        CURRENT_PRINCIPAL.reset(context)
    resources = OAuthResources(connection, ProjectAccess(store))
    binding = await resources.bind("design", user, project_id=project["id"], resource=origin + "/mcp")
    return resources, connection, user, binding, run


async def test_native_resource_binding_is_safe_and_checks_owned_project(store, tmp_path):
    async with local_provider() as (origin, _, _, _, tokens):
        resources, _, user, binding, _ = await admitted(store, tmp_path, origin)
        assert binding["resource"] == origin + "/mcp" and binding["scopes"] == ["read"]
        assert binding["project_id"] and binding["grant_family"]
        assert all(
            token not in json.dumps(binding)
            for token in tokens[-1].values()
            if isinstance(token, str) and len(token) > 20
        )
        with pytest.raises(ValueError, match="project"):
            await resources.bind("design", user, project_id=uuid4().hex, resource=origin + "/mcp")
        with pytest.raises(ValueError, match="resource"):
            await resources.bind("design", user, project_id=binding["project_id"], resource=origin + "/another")


async def test_native_resource_transport_binds_exact_run_and_keeps_bearer_only_on_wire(store, tmp_path):
    import httpx2

    async with local_provider() as (origin, _, _, _, tokens):
        resources, _, _, binding, run = await admitted(store, tmp_path, origin)
        observed = []

        async def reply(request):
            observed.append(request.headers["authorization"] == "Bearer " + tokens[-1]["access_token"])
            return httpx2.Response(200, json={"owned": True})

        async with httpx2.AsyncClient(
            transport=resources.transport(binding, run_id=run["id"], inner=httpx2.MockTransport(reply))
        ) as client:
            assert (await client.post(origin + "/mcp", json={"jsonrpc": "2.0"})).status_code == 200
            with pytest.raises(ValueError, match="resource"):
                await client.post(origin + "/different", json={})
        assert observed == [True]


async def test_resource_transport_denies_ambient_native_unit_before_wire(store, tmp_path):
    import httpx2

    async with local_provider() as (origin, *_):
        resources, _, _, binding, run = await admitted(store, tmp_path, origin)
        observed = []

        async def reply(request):
            observed.append(1)
            return httpx2.Response(200)

        async with (
            httpx2.AsyncClient(
                transport=resources.transport(binding, run_id=run["id"], inner=httpx2.MockTransport(reply))
            ) as client,
            store._unit(),
        ):
            with pytest.raises(ValueError, match="ambient"):
                await client.post(origin + "/mcp", json={})
        assert observed == []


@pytest.mark.parametrize("change", ["signature", "registration", "unknown", "owner_disabled", "expired"])
async def test_current_native_resource_fences_reject_before_bearer_dispatch(store, tmp_path, change):
    from datetime import timedelta

    import httpx2
    from sqlalchemy import select
    from test_connection_oauth_directory import effect

    from firefly_forge.connection_oauth_persistence import OAuthGrantEntity
    from firefly_forge.identity_persistence import ForgeIdentityUserEntity

    async with local_provider() as (origin, *_):
        resources, connection, user, binding, run = await admitted(store, tmp_path, origin)
        if change == "signature":
            binding = {**binding, "project_id": uuid4().hex}
        elif change == "registration":
            await connection.configure(
                "design",
                OAuthConnectionInput(
                    provider="fixture", client_id="client", public_client=True, project_id=binding["project_id"]
                ),
                user,
            )
        elif change == "unknown":
            await effect(store)
        elif change == "owner_disabled":
            async with store._unit() as session:
                session.add(
                    ForgeIdentityUserEntity(id=user.id, issuer=user.issuer, subject=user.subject, enabled=False)
                )
        elif change == "expired":
            async with store._unit() as session:
                grant = await session.scalar(select(OAuthGrantEntity))
                grant.expires_at = await connection._time(session) - timedelta(seconds=1)
        observed = []

        async def reply(request):
            observed.append(1)
            return httpx2.Response(200)

        with pytest.raises((ValueError, PermissionError, AccessDenied)):
            async with httpx2.AsyncClient(
                transport=resources.transport(binding, run_id=run["id"], inner=httpx2.MockTransport(reply))
            ) as client:
                await client.post(origin + "/mcp", json={})
        assert observed == []


async def test_same_family_explicit_refresh_uses_current_token_without_changing_binding(store, tmp_path):
    import httpx2

    async with local_provider() as (origin, forms, _, _, tokens):
        resources, connection, user, binding, run = await admitted(store, tmp_path, origin)
        first_token = tokens[-1]["access_token"]
        await connection.refresh("design", uuid4().hex, user)
        assert len(forms) == 2
        observed = []

        async def reply(request):
            observed.append(request.headers["authorization"] == "Bearer " + tokens[-1]["access_token"])
            assert request.headers["authorization"] != "Bearer " + first_token
            return httpx2.Response(200)

        async with httpx2.AsyncClient(
            transport=resources.transport(binding, run_id=run["id"], inner=httpx2.MockTransport(reply))
        ) as client:
            assert (await client.post(origin + "/mcp", json={})).status_code == 200
        assert observed == [True] and len(forms) == 2


async def test_resource_http_releases_native_locks_and_rejects_postdispatch_owner_revocation(
    store, store_factory, tmp_path
):
    import asyncio
    from contextvars import Context

    import httpx2
    from sqlalchemy import text

    from firefly_forge.identity_persistence import ForgeIdentityUserEntity
    from firefly_forge.project_access import AccessDenied

    async with local_provider() as (origin, *_):
        resources, _, user, binding, run = await admitted(store, tmp_path, origin)
        observer = await store_factory(tmp_path / "observer" / "factory.db")
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def reply(request):
            calls.append(1)
            entered.set()
            await release.wait()
            return httpx2.Response(200)

        async with httpx2.AsyncClient(
            transport=resources.transport(binding, run_id=run["id"], inner=httpx2.MockTransport(reply))
        ) as client:
            pending = asyncio.create_task(client.post(origin + "/mcp", json={}))
            try:
                async with asyncio.timeout(5):
                    await entered.wait()

                async def revoke():
                    async with observer._unit() as session:
                        acquired = await session.scalar(
                            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))"),
                            {"key": store.schema + ":connection-oauth:design"},
                        )
                        assert acquired is True
                        session.add(
                            ForgeIdentityUserEntity(id=user.id, issuer=user.issuer, subject=user.subject, enabled=False)
                        )

                async with asyncio.timeout(5):
                    await asyncio.create_task(revoke(), context=Context())
                release.set()
                with pytest.raises(AccessDenied, match="suspended"):
                    await pending
                assert calls == [1]
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)


async def test_new_replica_uses_a_rotated_native_grant_without_a_local_token_cache(store, store_factory, tmp_path):
    import httpx2

    from firefly_forge.connection_oauth import ConnectionOAuth
    from firefly_forge.oauth_resources import OAuthResources

    async with local_provider() as (origin, forms, _, _, tokens):
        resources, writer, user, binding, run = await admitted(store, tmp_path, origin)
        replica = await store_factory(tmp_path / "reader" / "factory.db")
        key = writer.redaction_settings()["FORGE_CONNECTION_OAUTH_SECRET_ENCRYPTION_KEY"]
        reader = ConnectionOAuth(
            replica,
            encryption_key=key,
            browser_origin=writer.browser_origin,
            catalog=writer.catalog,
            allow_loopback_http=True,
        )
        await reader.start()
        new_resources = OAuthResources(reader, ProjectAccess(replica))
        await writer.refresh("design", uuid4().hex, user)
        observed = []

        async def reply(request):
            observed.append(request.headers["authorization"] == "Bearer " + tokens[-1]["access_token"])
            return httpx2.Response(200)

        async with httpx2.AsyncClient(
            transport=new_resources.transport(binding, run_id=run["id"], inner=httpx2.MockTransport(reply))
        ) as client:
            assert (await client.post(origin + "/mcp", json={})).status_code == 200
        assert observed == [True] and len(forms) == 2
