# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Signed login-token checks with controlled persistence and no database/network effects."""

import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa

from firefly_forge.identity import IdentityConfiguration, IdentityService, digest


class LoginRows:
    def __init__(self):
        self.rows = []

    async def save(self, row):
        if row not in self.rows:
            self.rows.append(row)

    async def state(self, value):
        return next((row for row in self.rows if row.state_hash == value), None)


class LoginStore:
    @asynccontextmanager
    async def _unit(self):
        yield


async def challenge(monkeypatch, *, audiences, azp="forge-ui", trusted=()):
    options = {"trusted_id_token_audiences": trusted} if trusted else {}
    config = IdentityConfiguration(
        issuer="https://id.example/realms/forge",
        client_id="forge-ui",
        api_audience="forge-api",
        redirect_uri="https://forge.example/api/v1/auth/callback",
        encryption_key=Fernet.generate_key().decode(),
        **options,
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="identity-test", alg="RS256", use="sig")
    attempts, sessions, calls, users = LoginRows(), LoginRows(), [], []
    pending = {}

    def token(audience, **extra):
        return jwt.encode(
            {
                "iss": config.issuer,
                "sub": "fixture-user",
                "aud": audience,
                "iat": int(time.time()),
                "exp": int(time.time()) + 300,
                **extra,
            },
            key,
            algorithm="RS256",
            headers={"kid": "identity-test"},
        )

    def transport(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/certs"):
            return httpx.Response(200, json={"keys": [jwk]})
        assert request.url.path.endswith("/token")
        values = parse_qs(request.content.decode())
        assert values["code_verifier"] == [pending["verifier"]]
        authorized_party = {"azp": azp} if azp is not None else {}
        return httpx.Response(
            200,
            json={
                "id_token": token(audiences, nonce=pending["nonce"], **authorized_party),
                "access_token": token("forge-api", typ="Bearer"),
                "token_type": "Bearer",
                "refresh_token": "fixture-only-refresh",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    service = IdentityService(LoginStore(), config=config, attempts=attempts, sessions=sessions, client=client)

    async def user(claims, **kwargs):
        users.append(claims["sub"])
        return SimpleNamespace(
            id=uuid4(),
            issuer=config.issuer,
            subject=claims["sub"],
            display_name=None,
            email=None,
            api_roles_audience=None,
            api_roles_expires_at=None,
        )

    monkeypatch.setattr(service, "_user", user)
    started = await service.begin_login()
    state = parse_qs(urlsplit(started.authorization_url).query)["state"][0]
    row = await attempts.state(digest(state))
    native = service._decrypt(row.encrypted_data)
    pending.update(nonce=native["oauth2_nonce"], verifier=native["oauth2_pkce_verifier"])
    return service, started, state, attempts, sessions, calls, users, client


@pytest.mark.parametrize("azp", [None, "forge-ui"])
async def test_untrusted_extra_id_audience_never_creates_session_or_replays_code(monkeypatch, azp):
    service, started, state, attempts, sessions, calls, users, client = await challenge(
        monkeypatch, audiences=["forge-ui", "untrusted-client"], azp=azp
    )
    try:
        with pytest.raises(ValueError, match="login failed"):
            await service.complete_login("fixture-code", state, started.browser_nonce)
        assert not sessions.rows and not users
        assert attempts.rows[0].consumed
        previous_calls = list(calls)
        with pytest.raises(ValueError, match="login failed"):
            await service.complete_login("fixture-code", state, started.browser_nonce)
        assert calls == previous_calls
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "audiences,azp,trusted",
    [
        ("forge-ui", None, ()),
        (["forge-ui"], None, ()),
        (["forge-ui", "trusted-client"], "forge-ui", ("trusted-client",)),
        (["forge-ui", "trusted-client"], None, ("trusted-client",)),
    ],
)
async def test_only_configured_id_audiences_allow_normal_native_login(monkeypatch, audiences, azp, trusted):
    service, started, state, _, sessions, _, users, client = await challenge(
        monkeypatch, audiences=audiences, azp=azp, trusted=trusted
    )
    try:
        result = await service.complete_login("fixture-code", state, started.browser_nonce)
        assert result.cookie and len(sessions.rows) == 1 and users == ["fixture-user"]
    finally:
        await client.aclose()


async def test_trusted_audiences_do_not_override_wrong_authorized_party(monkeypatch):
    service, started, state, _, sessions, _, users, client = await challenge(
        monkeypatch, audiences=["forge-ui", "trusted-client"], azp="other-client", trusted=("trusted-client",)
    )
    try:
        with pytest.raises(ValueError, match="login failed"):
            await service.complete_login("fixture-code", state, started.browser_nonce)
        assert not sessions.rows and not users
    finally:
        await client.aclose()


@pytest.mark.parametrize("value", ['"other-client"', '[""]', '[" padded"]', "[true]", "{}", "not-json"])
def test_invalid_environment_trust_policy_is_rejected(monkeypatch, value):
    monkeypatch.setenv("FORGE_AUTH_MODE", "local_bootstrap")
    monkeypatch.setenv("FORGE_API_TOKEN", "fixture-bootstrap-only")
    monkeypatch.setenv("FORGE_OIDC_TRUSTED_ID_TOKEN_AUDIENCES", value)
    with pytest.raises(ValueError):
        IdentityConfiguration.from_environment()


def test_environment_trust_policy_is_explicit_and_secret_values_stay_masked(monkeypatch):
    monkeypatch.setenv("FORGE_AUTH_MODE", "local_bootstrap")
    monkeypatch.setenv("FORGE_API_TOKEN", "fixture-bootstrap-only")
    monkeypatch.setenv("FORGE_OIDC_TRUSTED_ID_TOKEN_AUDIENCES", '["trusted-client"]')
    config = IdentityConfiguration.from_environment()
    assert config.trusted_id_token_audiences == ("trusted-client",)
    assert "fixture-bootstrap-only" not in config.model_dump_json()
