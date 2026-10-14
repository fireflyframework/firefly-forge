# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owned loopback HTTP authorization server, public pyfly client and native PG ledger."""

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
import uvicorn
from cryptography.fernet import Fernet
from pyfly.security.context import SecurityContext
from pyfly.security.oauth2.authorization_server import AuthorizationServer, InMemoryTokenStore
from pyfly.security.oauth2.client import ClientRegistration, InMemoryClientRegistrationRepository
from pyfly.security.oauth2.endpoints import AuthorizationServerEndpoints
from sqlalchemy import select
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from test_connection_oauth_ledger import actor


@asynccontextmanager
async def local_provider(
    *, delay=False, mutate=None, connection_name="design", callback_uri=None, scope="read", resource_path="/mcp"
):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
    callbacks, forms, entered, release = [], [], asyncio.Event(), asyncio.Event()
    native = AuthorizationServer(
        "fixture-signing-key-is-local-only-32-bytes",
        InMemoryClientRegistrationRepository(
            ClientRegistration(
                registration_id="client",
                client_id="client",
                client_secret="fixture-secret",
                redirect_uri=callback_uri
                or f"https://forge.example/api/v1/connection-oauth/{connection_name}/callback",
                scopes=[scope],
            )
        ),
        InMemoryTokenStore(),
        issuer=origin,
        audience=origin + resource_path,
    )

    async def token(request):
        form = dict(await request.form())
        forms.append({k: str(v) for k, v in form.items()})
        entered.set()
        if delay:
            await release.wait()
        response = await stock_token(request)
        if response.status_code >= 400:
            return response
        result = json.loads(response.body)
        if mutate:
            result = mutate(result)
        callbacks.append(result)
        return JSONResponse(result)

    class OwnerMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.security_context = SecurityContext(user_id="fixture-owner")
            return await call_next(request)

    routes = AuthorizationServerEndpoints(native).routes()
    stock_token = next(route.endpoint for route in routes if route.path == "/oauth2/token")
    app = Starlette(
        routes=[Route("/oauth2/token", token, methods=["POST"]), *[r for r in routes if r.path != "/oauth2/token"]]
    )
    app.add_middleware(OwnerMiddleware)
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        yield origin, forms, entered, release, callbacks
    finally:
        release.set()
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()


async def setup(store, origin):
    from firefly_forge.connection_oauth import ConnectionOAuth
    from firefly_forge.connection_oauth_models import OAuthConnectionInput, OAuthProvider

    policy = OAuthProvider(
        id="fixture",
        issuer=origin,
        authorization_endpoint=origin + "/oauth2/authorize",
        token_endpoint=origin + "/oauth2/token",
        resource=origin + "/mcp",
        scopes=("read",),
        scope_omission="requested",
        refresh_scope_omission="preserve",
    )
    key = Fernet.generate_key().decode()
    service = ConnectionOAuth(
        store,
        encryption_key=key,
        browser_origin="https://forge.example",
        catalog={"fixture": policy},
        allow_loopback_http=True,
    )
    user = actor()
    await service.configure(
        "design",
        OAuthConnectionInput(
            provider="fixture",
            client_id="client",
            client_secret="fixture-secret",
            authentication="client_secret_basic",
        ),
        user,
    )
    handoff = await service.prepare("design", user)
    challenge = await service.begin("design", handoff["ticket"], user, browser_authenticated=True)
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
        authorization = await client.get(challenge.authorization_url)
    assert authorization.status_code == 302
    callback = parse_qs(urlsplit(authorization.headers["location"]).query)
    return service, user, challenge, callback, key, policy


async def complete(service, user, challenge, callback):
    return await service.complete(
        "design",
        callback["state"][0],
        callback["code"][0],
        challenge.browser_nonce,
        user,
        issuer=callback["iss"][0],
    )


async def test_real_native_code_refresh_single_dispatch_and_secret_custody(store):
    async with local_provider() as (origin, forms, entered, release, tokens):
        svc, user, challenge, callback, _, _ = await setup(store, origin)
        first = await complete(svc, user, challenge, callback)
        assert first["status"] == "authorized_unverified" and first["agent_ready"] is False
        with pytest.raises(ValueError, match="consumed"):
            await complete(svc, user, challenge, callback)
        assert len(forms) == 1 and forms[0]["resource"] == origin + "/mcp"
        request_id = uuid4().hex
        refreshed = await svc.refresh("design", request_id, user)
        assert refreshed["grant_revision"] == 2 and len(forms) == 2
        assert await svc.refresh("design", request_id, user) == refreshed
        assert len(forms) == 2
        from firefly_forge.connection_oauth_persistence import OAuthEventEntity, OAuthGrantEntity

        async with store._unit() as session:
            rows = list(await session.scalars(select(OAuthGrantEntity)))
            events = list(await session.scalars(select(OAuthEventEntity)))
            serialized = json.dumps([event.data for event in events])
            assert rows[0].family == first["family"] and rows[0].revision == 2
            for secret in (
                callback["code"][0],
                challenge.browser_nonce,
                tokens[0]["access_token"],
                tokens[0]["refresh_token"],
                "fixture-secret",
            ):
                assert secret not in rows[0].encrypted_data and secret not in serialized


@pytest.mark.parametrize("wrong", ["nonce", "actor", "issuer"])
async def test_invalid_binding_never_dispatches_and_valid_attempt_still_works(store, wrong):
    async with local_provider() as (origin, forms, *_):
        svc, user, challenge, callback, _, _ = await setup(store, origin)
        with pytest.raises(ValueError):
            await svc.complete(
                "design",
                callback["state"][0],
                callback["code"][0],
                "wrong" if wrong == "nonce" else challenge.browser_nonce,
                actor() if wrong == "actor" else user,
                issuer="https://other.example" if wrong == "issuer" else origin,
            )
        assert not forms
        await complete(svc, user, challenge, callback)
        assert len(forms) == 1


async def test_two_instances_cannot_dispatch_concurrent_callback_or_refresh(store):
    async with local_provider(delay=True) as (origin, forms, entered, release, _):
        svc, user, challenge, callback, key, policy = await setup(store, origin)
        from firefly_forge.connection_oauth import ConnectionOAuth

        other = ConnectionOAuth(
            store,
            encryption_key=key,
            browser_origin="https://forge.example",
            catalog={"fixture": policy},
            allow_loopback_http=True,
        )
        pending = asyncio.create_task(complete(svc, user, challenge, callback))
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(ValueError, match="consumed"):
            await complete(other, user, challenge, callback)
        release.set()
        await pending
        entered.clear()
        release.clear()
        pending = asyncio.create_task(svc.refresh("design", uuid4().hex, user))
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(ValueError, match="executing"):
            await other.refresh("design", uuid4().hex, user)
        release.set()
        await pending
        assert len(forms) == 2


async def test_cancelled_code_is_unknown_and_cannot_replay_after_restart(store):
    async with local_provider(delay=True) as (origin, forms, entered, release, _):
        svc, user, challenge, callback, _, _ = await setup(store, origin)
        pending = asyncio.create_task(complete(svc, user, challenge, callback))
        await asyncio.wait_for(entered.wait(), 5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        with pytest.raises(ValueError, match="consumed"):
            await complete(svc, user, challenge, callback)
        assert (await svc.summary("design", user))["status"] == "reauthorization_required"
        assert len(forms) == 1


async def test_postdispatch_disconnect_never_publishes_grant(store):
    async with local_provider(delay=True) as (origin, forms, entered, release, _):
        svc, user, challenge, callback, _, _ = await setup(store, origin)
        pending = asyncio.create_task(complete(svc, user, challenge, callback))
        await asyncio.wait_for(entered.wait(), 5)
        await svc.disconnect("design", user)
        release.set()
        with pytest.raises(ValueError, match="changed"):
            await pending
        from firefly_forge.connection_oauth_persistence import OAuthGrantEntity

        async with store._unit() as session:
            assert await session.scalar(select(OAuthGrantEntity)) is None
        assert (await svc.summary("design", user))["status"] == "disconnected"


async def test_scope_escalation_is_rejected_and_never_treated_as_verified(store):
    async with local_provider(mutate=lambda token: {**token, "scope": "read write"}) as (origin, forms, *_):
        svc, user, challenge, callback, _, _ = await setup(store, origin)
        with pytest.raises(ValueError, match="scope"):
            await complete(svc, user, challenge, callback)
        assert (await svc.summary("design", user))["status"] == "reauthorization_required"
        assert len(forms) == 1


async def test_old_rotated_ciphertext_cannot_restore_consumed_refresh_token(store):
    async with local_provider() as (origin, forms, *_):
        svc, user, challenge, callback, _, _ = await setup(store, origin)
        await complete(svc, user, challenge, callback)
        from firefly_forge.connection_oauth_persistence import OAuthGrantEntity

        async with store._unit() as session:
            old_cipher = (await session.scalar(select(OAuthGrantEntity))).encrypted_data
        await svc.refresh("design", uuid4().hex, user)
        async with store._unit() as session:
            (await session.scalar(select(OAuthGrantEntity))).encrypted_data = old_cipher
        with pytest.raises(ValueError, match="context"):
            await svc.refresh("design", uuid4().hex, user)
        assert len(forms) == 2


async def test_cancelled_refresh_remains_unknown_after_restart_and_same_request_never_dispatches(store):
    async with local_provider(delay=True) as (origin, forms, entered, release, _):
        release.set()
        svc, user, challenge, callback, key, policy = await setup(store, origin)
        await complete(svc, user, challenge, callback)
        entered.clear()
        release.clear()
        request_id = uuid4().hex
        pending = asyncio.create_task(svc.refresh("design", request_id, user))
        await asyncio.wait_for(entered.wait(), 5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        from firefly_forge.connection_oauth import ConnectionOAuth

        other = ConnectionOAuth(
            store,
            encryption_key=key,
            browser_origin="https://forge.example",
            catalog={"fixture": policy},
            allow_loopback_http=True,
        )
        with pytest.raises(ValueError, match="unknown"):
            await other.refresh("design", request_id, user)
        with pytest.raises(ValueError, match="reauthorization"):
            await other.refresh("design", uuid4().hex, user)
        assert len(forms) == 2


async def test_expired_executing_effect_persists_unknown_without_clock_skew_or_network_replay(store, monkeypatch):
    async with local_provider() as (origin, forms, *_):
        svc, user, challenge, callback, _, _ = await setup(store, origin)
        await complete(svc, user, challenge, callback)
        from datetime import timedelta

        from sqlalchemy import text

        from firefly_forge.connection_oauth_persistence import OAuthConnectionEntity, OAuthEffectEntity

        async with store._unit() as session:
            row = await session.scalar(select(OAuthConnectionEntity))
            now = await session.scalar(text("SELECT clock_timestamp()"))
            effect = OAuthEffectEntity(
                connection_id=row.id,
                request_id=uuid4().hex,
                kind="refresh",
                state="executing",
                lease_expires_at=now - timedelta(seconds=1),
                data={"registration_revision": row.revision},
            )
            session.add(effect)
        monkeypatch.setattr("time.time", lambda: 0)
        with pytest.raises(ValueError, match="unknown"):
            await svc.refresh("design", uuid4().hex, user)
        async with store._unit() as session:
            assert (
                await session.scalar(select(OAuthEffectEntity.state).where(OAuthEffectEntity.id == effect.id))
                == "unknown"
            )
        assert len(forms) == 1


async def test_role_loss_after_dispatch_is_archived_unknown_and_never_publishes(store):
    async with local_provider(delay=True) as (origin, forms, entered, release, _):
        svc, user, challenge, callback, _, _ = await setup(store, origin)
        pending = asyncio.create_task(complete(svc, user, challenge, callback))
        await asyncio.wait_for(entered.wait(), 5)
        from firefly_forge.connection_oauth_persistence import OAuthEffectEntity, OAuthGrantEntity
        from firefly_forge.identity_persistence import ForgeIdentityUserEntity

        async with store._unit() as session:
            session.add(ForgeIdentityUserEntity(id=user.id, issuer=user.issuer, subject=user.subject, enabled=False))
        release.set()
        with pytest.raises(PermissionError):
            await pending
        async with store._unit() as session:
            assert await session.scalar(select(OAuthGrantEntity)) is None
            assert await session.scalar(select(OAuthEffectEntity.state)) == "unknown"
        assert len(forms) == 1


async def test_rejected_refresh_duplicate_retains_native_error_kind_without_replay(store):
    count = 0

    def rejection(result):
        nonlocal count
        count += 1
        return result if count == 1 else {"error": "invalid_grant"}

    async with local_provider(mutate=rejection) as (origin, forms, *_):
        svc, user, challenge, callback, _, _ = await setup(store, origin)
        await complete(svc, user, challenge, callback)
        from pyfly.oauth2 import OAuth2ClientError

        request_id = uuid4().hex
        for _ in range(2):
            with pytest.raises(OAuth2ClientError) as failed:
                await svc.refresh("design", request_id, user)
            assert failed.value.code == "invalid_grant"
        assert len(forms) == 2
