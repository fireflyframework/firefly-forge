# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import hashlib
import importlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from starlette.requests import Request


def identity():
    return importlib.import_module("firefly_forge.identity")


def request(*, cookie=None, bearer=None, origin=None):
    headers = []
    for name, value in (("cookie", cookie), ("authorization", bearer), ("origin", origin)):
        if value:
            headers.append((name.encode(), value.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/auth/session",
            "scheme": "https",
            "server": ("forge.example", 443),
            "client": ("127.0.0.1", 1234),
            "headers": headers,
        }
    )


async def fixture(store, *, wrong=None, uncertain=False, initial_iat=None):
    mod = identity()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="key-1", alg="RS256", use="sig")
    config = mod.IdentityConfiguration(
        mode="oidc",
        issuer="https://id.example/realms/forge",
        client_id="forge-ui",
        api_audience="forge-api",
        redirect_uri="https://forge.example/api/v1/auth/callback",
        encryption_key=Fernet.generate_key().decode(),
        client_secret="private-client-value",
    )
    calls, pending = [], {}

    def token(audience, *, nonce=None, roles=None, **extra):
        payload = {
            "iss": config.issuer,
            "sub": "user-1",
            "aud": audience,
            "exp": int(time.time()) + 300,
            "iat": int(time.time()),
            "typ": "ID" if nonce else "Bearer",
            "azp": "forge-ui",
            "email": "owner@example.test",
            "email_verified": True,
            "name": "Owner",
            "realm_access": {"roles": ["forge-admin"]},
            "resource_access": {"forge-api": {"roles": roles or ["forge-admin"]}},
        }
        if nonce:
            payload["nonce"] = nonce
        payload.update(extra)
        return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "key-1"})

    async def transport(req):
        calls.append(req.url.path)
        if req.url.path.endswith("/certs"):
            return httpx.Response(200, json={"keys": [jwk]})
        if req.url.path.endswith("/revoke"):
            return httpx.Response(200)
        if uncertain:
            raise httpx.ReadTimeout("remote private response unavailable", request=req)
        form = parse_qs(req.content.decode())
        assert form["code_verifier"][0] == pending["verifier"]
        assert form["client_secret"] == ["private-client-value"]
        overrides = {"nonce": "incorrect"} if wrong == "nonce" else {}
        if wrong == "issuer":
            overrides["iss"] = "https://another.example"
        issuance = {"iat": initial_iat} if initial_iat is not None else {}
        return httpx.Response(
            200,
            json={
                "id_token": token("forge-ui", **({"nonce": pending["nonce"]} | overrides | issuance)),
                "access_token": token("wrong-audience" if wrong == "audience" else "forge-api", **issuance),
                "refresh_token": "private-refresh-value",
                "expires_in": 300,
                "token_type": "Bearer",
            },
        )

    service = mod.IdentityService(
        store, config=config, client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
    )
    challenge = await service.begin_login()
    params = parse_qs(urlsplit(challenge.authorization_url).query)
    from firefly_forge.identity_persistence import ForgeIdentityAttemptEntity

    async with store._unit() as unit:
        row = await unit.scalar(select(ForgeIdentityAttemptEntity))
        native = json.loads(service.cipher.decrypt(row.encrypted_data.encode()))
        pending.update(nonce=params["nonce"][0], verifier=native["oauth2_pkce_verifier"])
    return service, challenge, params, calls, token


async def test_native_pkce_login_persists_only_encrypted_tokens_and_hashed_cookies(store):
    service, challenge, params, calls, _ = await fixture(store)
    assert params["code_challenge_method"] == ["S256"]
    session = await service.complete_login(
        "code-1", params["state"][0], challenge.browser_nonce, issuer=service.config.issuer
    )
    principal = await service.authenticate(request(cookie="forge_session=" + session.cookie))
    assert principal and principal.id == session.principal.id and principal.api_roles == frozenset({"forge-admin"})
    assert principal.issuer == service.config.issuer and principal.subject == "user-1"
    assert "/realms/forge/protocol/openid-connect/token" in calls
    from firefly_forge.identity_persistence import ForgeIdentitySessionEntity

    async with store._unit() as unit:
        row = await unit.scalar(select(ForgeIdentitySessionEntity))
        assert row.cookie_hash == hashlib.sha256(session.cookie.encode()).hexdigest()
        assert "private-refresh-value" not in row.encrypted_data and session.cookie not in row.encrypted_data
    assert "private-client-value" not in json.dumps(service.configuration())
    await service.logout(session.cookie)
    assert await service.authenticate(request(cookie="forge_session=" + session.cookie)) is None


@pytest.mark.parametrize("wrong", ["nonce", "issuer", "audience"])
async def test_wrong_claims_never_create_session_and_attempt_cannot_be_replayed(store, wrong):
    service, challenge, params, calls, _ = await fixture(store, wrong=wrong)
    with pytest.raises(ValueError, match="login"):
        await service.complete_login(
            "code-1", params["state"][0], challenge.browser_nonce, issuer=service.config.issuer
        )
    count = len(calls)
    with pytest.raises(ValueError, match="login"):
        await service.complete_login(
            "code-1", params["state"][0], challenge.browser_nonce, issuer=service.config.issuer
        )
    assert len(calls) == count


async def test_cookie_binding_and_unknown_exchange_never_repeat_token_request(store):
    service, challenge, params, calls, _ = await fixture(store, uncertain=True)
    with pytest.raises(ValueError, match="login"):
        await service.complete_login("code-1", params["state"][0], "incorrect", issuer=service.config.issuer)
    assert not calls
    with pytest.raises(ValueError, match="login"):
        await service.complete_login(
            "code-1", params["state"][0], challenge.browser_nonce, issuer=service.config.issuer
        )
    count = len(calls)
    with pytest.raises(ValueError, match="login"):
        await service.complete_login(
            "code-1", params["state"][0], challenge.browser_nonce, issuer=service.config.issuer
        )
    assert len(calls) == count


async def test_access_token_requires_api_audience_and_never_maps_realm_roles(store):
    service, _, _, _, token = await fixture(store)
    issued = int(time.time()) - 10
    assert await service.authenticate(request(bearer="Bearer " + token("forge-ui"))) is None
    principal = await service.authenticate(
        request(bearer="Bearer " + token("forge-api", roles=["irrelevant"], iat=issued))
    )
    assert principal and not principal.api_roles
    principal2 = await service.authenticate(
        request(bearer="Bearer " + token("forge-api", roles=["forge-worker"], iat=issued + 1))
    )
    assert principal2.id == principal.id and principal2.api_roles == frozenset({"forge-worker"})
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository

    async with store._unit():
        row = await ForgeIdentityUserRepository().find_by_id(principal.id)
        row.enabled = False
        await ForgeIdentityUserRepository().save(row)
    assert await service.authenticate(request(bearer="Bearer " + token("forge-api"))) is None


def test_production_requires_shared_key_https_and_explicit_audiences():
    mod = identity()
    with pytest.raises(ValueError):
        mod.IdentityConfiguration(
            mode="oidc",
            issuer="http://id.example",
            client_id="x",
            api_audience="x",
            redirect_uri="https://forge.example/callback",
        )
    with pytest.raises(ValueError):
        mod.IdentityConfiguration(
            mode="oidc",
            issuer="https://id.example",
            client_id="x",
            api_audience="x",
            redirect_uri="https://forge.example/callback",
        )


async def login(service, challenge, params):
    return await service.complete_login(
        "code-1", params["state"][0], challenge.browser_nonce, issuer=service.config.issuer
    )


async def expire_access(store, service, cookie):
    from datetime import UTC, datetime, timedelta

    async with store._unit():
        row = await service.sessions.cookie(identity().digest(cookie))
        row.access_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await service.sessions.save(row)


async def test_refresh_updates_validated_roles_and_rotates_encrypted_refresh_token(store):
    service, challenge, params, _, token = await fixture(store, initial_iat=int(time.time()) - 10)
    session = await login(service, challenge, params)
    await expire_access(store, service, session.cookie)
    refreshes = []

    async def transport(req):
        form = parse_qs(req.content.decode())
        refreshes.append(form)
        assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["private-refresh-value"]
        return httpx.Response(
            200,
            json={
                "access_token": token("forge-api", roles=["forge-worker"]),
                "refresh_token": "rotated-private-refresh",
                "token_type": "Bearer",
            },
        )

    service.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    principal = await service.authenticate(request(cookie="forge_session=" + session.cookie))
    assert principal.api_roles == frozenset({"forge-worker"}) and len(refreshes) == 1
    async with store._unit():
        row = await service.sessions.cookie(identity().digest(session.cookie))
        assert row.status == "active" and row.revision == 2
        assert service._decrypt(row.encrypted_data)["refresh_token"] == "rotated-private-refresh"
        assert "rotated-private-refresh" not in row.encrypted_data


@pytest.mark.parametrize("failure", ["uncertain", "logout", "subject"])
async def test_refresh_never_replays_unknown_outcome_or_resurrects_revoked_session(store, failure):
    service, challenge, params, _, token = await fixture(store)
    session = await login(service, challenge, params)
    await expire_access(store, service, session.cookie)
    calls = []

    async def transport(req):
        if req.url.path.endswith("/revoke"):
            return httpx.Response(200)
        calls.append(req.url.path)
        if failure == "uncertain":
            raise httpx.ReadTimeout("private unknown outcome", request=req)
        if failure == "logout":
            await service.logout(session.cookie)
        return httpx.Response(
            200,
            json={
                "access_token": token("forge-api", sub="another-user" if failure == "subject" else "user-1"),
                "refresh_token": "rotated-private",
                "token_type": "Bearer",
            },
        )

    service.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    assert await service.authenticate(request(cookie="forge_session=" + session.cookie)) is None
    assert await service.authenticate(request(cookie="forge_session=" + session.cookie)) is None
    assert len(calls) == 1
    async with store._unit():
        row = await service.sessions.cookie(identity().digest(session.cookie))
        assert row.status == "revoked"


async def test_concurrent_callback_consumes_one_attempt_before_one_remote_request(store):
    import asyncio

    service, challenge, params, calls, _ = await fixture(store)
    results = await asyncio.gather(
        login(service, challenge, params), login(service, challenge, params), return_exceptions=True
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert sum(path.endswith("/token") for path in calls) == 1


async def test_cookie_mutations_reject_cross_origin_and_missing_origin(store):
    mod = identity()
    config = mod.IdentityConfiguration(mode="local_bootstrap", bootstrap_token="test-local-only")
    service = mod.IdentityService(store, config=config)
    with pytest.raises(ValueError, match="origin"):
        service.check_cookie_origin(request(origin="https://untrusted.example"))


async def test_local_bootstrap_is_explicit_and_cannot_authenticate_remote_client(store):
    mod = identity()
    config = mod.IdentityConfiguration(mode="local_bootstrap", bootstrap_token="test-local-only")
    service = mod.IdentityService(store, config=config)
    local = request(bearer="Bearer test-local-only")
    assert (await service.authenticate(local)).kind == "local_bootstrap"
    local.scope["client"] = ("203.0.113.10", 1234)
    assert await service.authenticate(local) is None


async def test_bootstrap_login_uses_durable_opaque_cookie_without_browser_token(store):
    mod = identity()
    config = mod.IdentityConfiguration(mode="local_bootstrap", bootstrap_token="test-local-only")
    service = mod.IdentityService(store, config=config)
    with pytest.raises(ValueError):
        await service.login_bootstrap("incorrect", request())
    result = await service.login_bootstrap("test-local-only", request())
    assert result.cookie != "test-local-only"
    principal = await service.authenticate(request(cookie="forge_session=" + result.cookie))
    assert principal and principal.kind == "local_bootstrap"
    service2 = mod.IdentityService(store, config=config)
    assert await service2.authenticate(request(cookie="forge_session=" + result.cookie))
    await service2.logout(result.cookie)
    assert await service.authenticate(request(cookie="forge_session=" + result.cookie)) is None


async def test_native_controller_cookie_flags_fixed_redirect_and_generic_failure(store):
    from firefly_forge.identity_controllers import IdentityController, IdentityLoginInput

    service, _, _, _, _ = await fixture(store)
    controller = IdentityController(service)
    response = await controller.login(request(origin="https://forge.example"), IdentityLoginInput())
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert "Path=/api/v1/auth/callback" in cookie and response.status_code == 200
    assert json.loads(response.body)["authorization_url"].startswith(
        service.config.issuer + "/protocol/openid-connect/auth?"
    )
    failed = await controller.callback(request(), code="invalid", state="invalid", iss=service.config.issuer)
    assert failed.status_code == 401 and b"private" not in failed.body
    assert "Max-Age=0" in failed.headers["set-cookie"]
    with pytest.raises(ValueError, match="origin"):
        await controller.login(request(origin="https://attacker.example"), IdentityLoginInput())


async def test_bootstrap_controller_cookie_and_logout_never_return_operator_token(store):
    from firefly_forge.identity_controllers import IdentityController, IdentityLoginInput

    mod = identity()
    service = mod.IdentityService(
        store, config=mod.IdentityConfiguration(mode="local_bootstrap", bootstrap_token="test-local-only")
    )
    controller = IdentityController(service)
    response = await controller.login(
        request(origin="https://forge.example"), IdentityLoginInput(token="test-local-only")
    )
    assert response.status_code == 200 and b"test-local-only" not in response.body
    cookie = response.headers["set-cookie"].split(";", 1)[0]
    assert await service.authenticate(request(cookie=cookie))
    await controller.logout(request(cookie=cookie, origin="https://forge.example"))
    assert await service.authenticate(request(cookie=cookie)) is None


async def test_exact_external_browser_origin_allows_proxy_without_trusting_other_ports(store):
    mod = identity()
    config = mod.IdentityConfiguration(
        mode="local_bootstrap",
        bootstrap_token="test-local-only",
        browser_origin="http://127.0.0.1:5173",
        allow_loopback_http=True,
    )
    service = mod.IdentityService(store, config=config)
    service.check_cookie_origin(request(origin="http://127.0.0.1:5173"))
    assert service.configuration()["browser_origin"] == "http://127.0.0.1:5173"
    for origin in (None, "http://127.0.0.1:5174", "http://localhost:5173", "https://attacker.example"):
        with pytest.raises(ValueError, match="origin"):
            service.check_cookie_origin(request(origin=origin))


@pytest.mark.parametrize(
    "origin",
    [
        "https://forge.example/path",
        "https://user@forge.example",
        "https://forge.example?token=hidden",
        "http://forge.example",
        "null",
    ],
)
def test_browser_origin_configuration_rejects_paths_credentials_queries_and_non_tls(origin):
    with pytest.raises(ValueError):
        identity().IdentityConfiguration(
            mode="local_bootstrap", bootstrap_token="test-local-only", browser_origin=origin
        )
