# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Strict native PyFly OIDC authentication; project authorization belongs to the ACL."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pyfly.container import service
from pyfly.security.authentication import Authentication, AuthenticationProvider, ProviderManager
from pyfly.security.oauth2.client import InMemoryClientRegistrationRepository, keycloak
from pyfly.security.oauth2.login import OAuth2LoginHandler
from pyfly.security.oauth2.resource_server import ClaimMappings, JWKSTokenValidator
from pyfly.session.session import HttpSession
from sqlalchemy.dialects.postgresql import insert
from starlette.requests import Request

from firefly_forge.identity_persistence import (
    ForgeIdentityAttemptEntity,
    ForgeIdentityAttemptRepository,
    ForgeIdentitySessionEntity,
    ForgeIdentitySessionRepository,
    ForgeIdentityUserEntity,
    ForgeIdentityUserRepository,
)
from firefly_forge.private_identity import private_identity
from firefly_forge.store import Store

SESSION_COOKIE = "forge_session"
ATTEMPT_COOKIE = "forge_oidc_attempt"
AUTH_PREFIX = "/api/v1/auth"


@dataclass(frozen=True)
class Principal:
    id: UUID
    issuer: str
    subject: str
    display_name: str | None = None
    email: str | None = None
    authentication_method: Literal[
        "oidc_session", "oidc_bearer", "local_bootstrap", "scm_mapping", "personal_token"
    ] = "oidc_bearer"
    api_roles: frozenset[str] = field(default_factory=frozenset)
    kind: Literal["human", "service", "local_bootstrap"] = "human"
    api_audience: str | None = None
    scm_delegation: dict[str, Any] | None = None
    personal_token_id: UUID | None = None


class IdentityConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["oidc", "local_bootstrap"] = "oidc"
    issuer: str = ""
    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    api_audience: str = ""
    trusted_id_token_audiences: tuple[str, ...] = ()
    redirect_uri: str = ""
    browser_origin: str = ""
    encryption_key: SecretStr = SecretStr("")
    allow_loopback_http: bool = False
    session_seconds: int = Field(default=28800, ge=300, le=86400)
    bootstrap_token: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def validate_configuration(self) -> IdentityConfiguration:
        if any(not audience or audience != audience.strip() for audience in self.trusted_id_token_audiences):
            raise ValueError("Trusted ID-token audiences must be exact nonempty client identifiers")
        if self.browser_origin:
            origin = urlsplit(self.browser_origin)
            if (
                not origin.hostname
                or origin.username
                or origin.password
                or origin.path
                or origin.query
                or origin.fragment
                or origin.scheme != "https"
                and not (
                    self.allow_loopback_http
                    and origin.scheme == "http"
                    and origin.hostname in {"127.0.0.1", "localhost", "::1"}
                )
            ):
                raise ValueError(
                    "Browser origin requires an exact HTTPS origin or explicit development loopback origin"
                )
        if self.mode == "local_bootstrap":
            if not self.bootstrap_token.get_secret_value():
                raise ValueError("Local bootstrap requires an explicit operator credential")
            return self
        for value in (self.issuer, self.redirect_uri):
            parsed = urlsplit(value)
            if (
                not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.scheme != "https"
                and not (
                    self.allow_loopback_http
                    and parsed.scheme == "http"
                    and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                )
            ):
                raise ValueError("OIDC requires HTTPS endpoints; loopback HTTP is an explicit development option")
        if self.issuer.endswith("/") or not self.client_id or not self.api_audience:
            raise ValueError("OIDC requires an exact issuer, client ID and API audience")
        try:
            Fernet(self.encryption_key.get_secret_value().encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("OIDC requires a shared identity encryption key") from exc
        return self

    @classmethod
    def from_environment(cls) -> IdentityConfiguration:
        values = {
            "mode": os.getenv("FORGE_AUTH_MODE", "oidc"),
            "issuer": os.getenv("FORGE_OIDC_ISSUER", ""),
            "client_id": os.getenv("FORGE_OIDC_CLIENT_ID", ""),
            "client_secret": os.getenv("FORGE_OIDC_CLIENT_SECRET", ""),
            "api_audience": os.getenv("FORGE_OIDC_API_AUDIENCE", ""),
            "redirect_uri": os.getenv("FORGE_OIDC_REDIRECT_URI", ""),
            "browser_origin": os.getenv("FORGE_BROWSER_ORIGIN", ""),
            "encryption_key": os.getenv("FORGE_IDENTITY_ENCRYPTION_KEY", ""),
            "allow_loopback_http": os.getenv("FORGE_OIDC_ALLOW_LOOPBACK_HTTP") == "true",
            "bootstrap_token": os.getenv("FORGE_API_TOKEN", ""),
        }
        try:
            values["trusted_id_token_audiences"] = json.loads(os.getenv("FORGE_OIDC_TRUSTED_ID_TOKEN_AUDIENCES", "[]"))
            return cls.model_validate(values)
        except ValueError:
            raise ValueError(
                "Configure the OIDC issuer, client, audience, callback and shared encryption key"
            ) from None


@dataclass(frozen=True)
class LoginChallenge:
    authorization_url: str
    browser_nonce: str
    expires_in: int = 600


@dataclass(frozen=True)
class LoginSession:
    cookie: str
    principal: Principal
    expires_in: int


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@service
class IdentityService:
    def __init__(
        self,
        store: Store,
        config: IdentityConfiguration | None = None,
        users: ForgeIdentityUserRepository | None = None,
        sessions: ForgeIdentitySessionRepository | None = None,
        attempts: ForgeIdentityAttemptRepository | None = None,
        client: httpx.AsyncClient | None = None,
        personal_token_provider: AuthenticationProvider | None = None,
    ) -> None:
        self.store, self.config = store, config or IdentityConfiguration.from_environment()
        self.users, self.sessions = users or ForgeIdentityUserRepository(), sessions or ForgeIdentitySessionRepository()
        self.attempts = attempts or ForgeIdentityAttemptRepository()
        self.client = client or httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False)
        self.owns_client = client is None
        key = self.config.encryption_key.get_secret_value().encode()
        if not key and self.config.mode == "local_bootstrap":
            path = self.store.path.parent / "identity-bootstrap.key"
            path.parent.mkdir(parents=True, exist_ok=True)
            key = private_identity(path, Fernet.generate_key, size=44)
        self.cipher = Fernet(key)
        self.encryption_key = key.decode()
        self.registration = (
            keycloak(
                self.config.client_id,
                self.config.client_secret.get_secret_value(),
                self.config.issuer,
                self.config.redirect_uri,
            )
            if self.config.mode == "oidc"
            else None
        )
        self.validators: dict[str, JWKSTokenValidator] = {}
        self.personal_token_manager = ProviderManager(personal_token_provider) if personal_token_provider else None

    async def stop(self) -> None:
        if self.owns_client:
            await self.client.aclose()

    def redaction_settings(self) -> dict[str, str]:
        return {
            "FORGE_OIDC_CLIENT_SECRET": self.config.client_secret.get_secret_value(),
            "FORGE_IDENTITY_ENCRYPTION_KEY": self.encryption_key,
            "FORGE_API_TOKEN": self.config.bootstrap_token.get_secret_value(),
        }

    def configuration(self) -> dict[str, Any]:
        return {
            "mode": self.config.mode,
            "issuer": self.config.issuer,
            "client_id": self.config.client_id,
            "api_audience": self.config.api_audience,
            "trusted_id_token_audiences": list(self.config.trusted_id_token_audiences),
            "redirect_uri": self.config.redirect_uri,
            "browser_origin": self.config.browser_origin
            or (
                f"{urlsplit(self.config.redirect_uri).scheme}://{urlsplit(self.config.redirect_uri).netloc}"
                if self.config.mode == "oidc"
                else None
            ),
            "session_seconds": self.config.session_seconds,
            "login_path": AUTH_PREFIX + "/login",
            "callback_path": AUTH_PREFIX + "/callback",
            "session_path": AUTH_PREFIX + "/session",
            "logout_path": AUTH_PREFIX + "/logout",
            "cookie_secure": urlsplit(self.config.redirect_uri).scheme == "https",
        }

    def _encrypt(self, value: dict[str, Any]) -> str:
        assert self.cipher is not None
        return self.cipher.encrypt(json.dumps(value, separators=(",", ":")).encode()).decode()

    def _decrypt(self, value: str) -> dict[str, Any]:
        assert self.cipher is not None
        try:
            result = json.loads(self.cipher.decrypt(value.encode()))
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (InvalidToken, ValueError, TypeError) as exc:
            raise ValueError("Identity session unavailable; sign in again") from exc

    async def begin_login(self) -> LoginChallenge:
        if self.registration is None:
            raise ValueError("OIDC login is not enabled")
        session = HttpSession(secrets.token_urlsafe(32), is_new=True)
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [],
                "path_params": {"registration_id": self.registration.registration_id},
            }
        )
        request.state.session = session
        handler = OAuth2LoginHandler(InMemoryClientRegistrationRepository(self.registration))
        response = await handler.routes()[0].endpoint(request)
        nonce = secrets.token_urlsafe(32)
        values = {
            name: session.get_attribute(name) for name in ("oauth2_state", "oauth2_nonce", "oauth2_pkce_verifier")
        }
        if not all(isinstance(value, str) for value in values.values()):
            raise ValueError("Native OIDC challenge is unavailable")
        async with self.store._unit():
            await self.attempts.save(
                ForgeIdentityAttemptEntity(
                    state_hash=digest(str(values["oauth2_state"])),
                    browser_nonce_hash=digest(nonce),
                    expires_at=datetime.now(UTC) + timedelta(seconds=600),
                    consumed=False,
                    encrypted_data=self._encrypt(values),
                )
            )
        return LoginChallenge(response.headers["location"], nonce)

    async def _bootstrap_user(self) -> ForgeIdentityUserEntity:
        user_id = uuid5(NAMESPACE_URL, "firefly-forge:local-bootstrap")
        async with self.store._unit() as unit:
            await unit.execute(
                insert(ForgeIdentityUserEntity)
                .values(id=user_id, issuer="local:bootstrap", subject="operator", enabled=True)
                .on_conflict_do_nothing()
            )
            user = await self.users.subject("local:bootstrap", "operator")
            if (
                user is None
                or user.id != user_id
                or (user.issuer, user.subject) != ("local:bootstrap", "operator")
                or not user.enabled
            ):
                raise ValueError("Local bootstrap identity is unavailable or suspended")
            return user

    async def login_bootstrap(self, token: str, request: Request) -> LoginSession:
        if (
            self.config.mode != "local_bootstrap"
            or not request.client
            or request.client.host not in {"127.0.0.1", "::1"}
            or len(token) > 16384
            or not hmac.compare_digest(token.encode(), self.config.bootstrap_token.get_secret_value().encode())
        ):
            raise ValueError("Local bootstrap login failed")
        async with self.store._unit():
            user = await self._bootstrap_user()
            cookie = secrets.token_urlsafe(48)
            expiry = datetime.now(UTC) + timedelta(seconds=self.config.session_seconds)
            await self.sessions.save(
                ForgeIdentitySessionEntity(
                    user_id=user.id,
                    cookie_hash=digest(cookie),
                    status="active",
                    revision=1,
                    expires_at=expiry,
                    access_expires_at=expiry,
                    encrypted_data=self._encrypt({"bootstrap_token_hash": digest(token)}),
                )
            )
        principal = Principal(
            user.id, user.issuer, user.subject, authentication_method="local_bootstrap", kind="local_bootstrap"
        )
        return LoginSession(cookie, principal, self.config.session_seconds)

    async def _json(
        self, method: str, url: str, *, form: dict[str, str] | None = None, limit: int = 262144
    ) -> dict[str, Any]:
        async with self.client.stream(
            method, url, data=form, headers={"Accept-Encoding": "identity"}, timeout=10, follow_redirects=False
        ) as response:
            if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                raise ValueError("Identity provider request failed")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    raise ValueError("Identity provider response exceeds its limit")
            doc = json.loads(body)
            if not isinstance(doc, dict):
                raise ValueError("Identity provider response is invalid")
            return doc

    async def _jwks(self, uri: str, *, timeout: float, max_bytes: int) -> bytes:
        assert self.registration is not None
        if uri != self.registration.jwks_uri:
            raise ValueError("Identity key endpoint mismatch")
        return json.dumps(await self._json("GET", uri, limit=max_bytes)).encode()

    async def _claims(self, token: str, audience: str) -> dict[str, Any]:
        if not self.registration:
            raise ValueError("OIDC is not enabled")
        if audience not in self.validators:
            self.validators[audience] = JWKSTokenValidator(
                self.registration.jwks_uri,
                issuer=self.config.issuer,
                audiences=[audience],
                algorithms=["RS256"],
                leeway=15,
                async_jwks_fetcher=self._jwks,
                jwks_timeout=10,
                jwks_cache_seconds=300,
                jwks_min_refresh_seconds=1,
                claim_mappings=ClaimMappings(
                    principal_claims=("sub",),
                    authority_claims=(),
                    scope_claims=(),
                ),
            )
        claims = await self.validators[audience].validate_async(token, timeout=10)
        if not isinstance(claims.get("sub"), str) or not 1 <= len(claims["sub"]) <= 255:
            raise ValueError("Invalid authenticated subject")
        if not isinstance(claims.get("iat"), (int, float)) or claims["iat"] > datetime.now(UTC).timestamp() + 15:
            raise ValueError("Invalid identity token issuance time")
        return claims

    async def _access(self, token: str) -> dict[str, Any]:
        claims = await self._claims(token, self.config.api_audience)
        if claims.get("typ") != "Bearer":
            raise ValueError("An API access token is required")
        return claims

    def _api_grants(self, claims: dict[str, Any]) -> frozenset[str]:
        access = claims.get("resource_access", {})
        client = access.get(self.config.api_audience, {}) if isinstance(access, dict) else {}
        roles = client.get("roles", []) if isinstance(client, dict) else []
        return (
            frozenset(role for role in roles if isinstance(role, str) and role in {"forge-admin", "forge-worker"})
            if isinstance(roles, list)
            else frozenset()
        )

    def _record_authority(self, user: ForgeIdentityUserEntity, claims: dict[str, Any]) -> None:
        issued, expires = claims.get("iat"), claims.get("exp")
        if (
            isinstance(issued, bool)
            or not isinstance(issued, (int, float))
            or not math.isfinite(issued)
            or isinstance(expires, bool)
            or not isinstance(expires, (int, float))
            or not math.isfinite(expires)
        ):
            return
        issued = float(issued)
        expiration = datetime.fromtimestamp(float(expires), UTC)
        if user.api_roles_issued_at is not None and issued < user.api_roles_issued_at:
            return
        grants = self._api_grants(claims)
        if issued == user.api_roles_issued_at:
            # Tokens issued in the same second cannot restore a grant already observed as removed.
            grants &= (
                frozenset(user.api_roles or ()) if user.api_roles_audience == self.config.api_audience else frozenset()
            )
            if user.api_roles_expires_at is not None:
                expiration = min(expiration, user.api_roles_expires_at)
        user.api_roles = sorted(grants)
        user.api_roles_audience = self.config.api_audience
        user.api_roles_issued_at = issued
        user.api_roles_expires_at = expiration

    def _principal(
        self, user: ForgeIdentityUserEntity, claims: dict[str, Any], method: Literal["oidc_session", "oidc_bearer"]
    ) -> Principal:
        grants = self._api_grants(claims)
        if (
            user.api_roles_audience != self.config.api_audience
            or user.api_roles_expires_at is None
            or user.api_roles_expires_at <= datetime.now(UTC)
        ):
            grants = frozenset()
        else:
            grants &= frozenset(user.api_roles or ())
        return Principal(
            user.id,
            user.issuer,
            user.subject,
            user.display_name,
            user.email,
            authentication_method=method,
            api_roles=grants,
            api_audience=self.config.api_audience,
        )

    async def _user(
        self, claims: dict[str, Any], *, authority_claims: dict[str, Any] | None = None
    ) -> ForgeIdentityUserEntity:
        name, email = claims.get("name"), claims.get("email")
        name = name[:200] if isinstance(name, str) else None
        if not name:
            username = claims.get("preferred_username")
            name = username[:200] if isinstance(username, str) else None
        email = email[:320] if isinstance(email, str) and claims.get("email_verified") is True else None
        async with self.store._unit() as unit:
            await unit.execute(
                insert(ForgeIdentityUserEntity)
                .values(
                    id=uuid4(),
                    issuer=self.config.issuer,
                    subject=claims["sub"],
                    enabled=True,
                    display_name=name,
                    email=email,
                )
                .on_conflict_do_nothing(index_elements=["issuer", "subject"])
            )
            user = await self.users.subject(self.config.issuer, claims["sub"])
            if user is None or not user.enabled:
                raise ValueError("Identity is suspended")
            user.display_name, user.email = name or user.display_name, email
            authority = authority_claims if authority_claims is not None else claims
            if authority.get("typ") == "Bearer":
                if authority.get("sub") != user.subject:
                    raise ValueError("Identity authority subject mismatch")
                self._record_authority(user, authority)
            await self.users.save(user)
            return user

    async def complete_login(
        self, code: str, state: str, browser_nonce: str, *, issuer: str | None = None
    ) -> LoginSession:
        if not self.registration or not all(
            isinstance(s, str) and 1 <= len(s) <= 4096 for s in (code, state, browser_nonce)
        ):
            raise ValueError("OIDC login failed; begin a new login")
        if issuer is not None and issuer != self.config.issuer:
            raise ValueError("OIDC login failed; begin a new login")
        async with self.store._unit():
            attempt = await self.attempts.state(digest(state))
            if (
                attempt is None
                or attempt.consumed
                or attempt.expires_at <= datetime.now(UTC)
                or not hmac.compare_digest(attempt.browser_nonce_hash, digest(browser_nonce))
            ):
                raise ValueError("OIDC login failed; begin a new login")
            attempt.consumed = True
            native = self._decrypt(attempt.encrypted_data)
            await self.attempts.save(attempt)
        try:
            tokens = await self._json(
                "POST",
                self.registration.token_uri,
                form={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.config.redirect_uri,
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret.get_secret_value(),
                    "code_verifier": native["oauth2_pkce_verifier"],
                },
            )
            id_claims = await self._claims(tokens["id_token"], self.config.client_id)
            audiences = id_claims.get("aud")
            audience_values = audiences if isinstance(audiences, list) else [audiences]
            trusted = {self.config.client_id, *self.config.trusted_id_token_audiences}
            if any(audience not in trusted for audience in audience_values):
                raise ValueError("ID token contains an untrusted audience")
            if not hmac.compare_digest(str(id_claims.get("nonce", "")), native["oauth2_nonce"]):
                raise ValueError("Nonce mismatch")
            if id_claims.get("azp", self.config.client_id) != self.config.client_id:
                raise ValueError("Authorized party mismatch")
            claims = await self._access(tokens["access_token"])
            if claims["sub"] != id_claims["sub"] or tokens.get("token_type", "").lower() != "bearer":
                raise ValueError("Token subject mismatch")
            user = await self._user(id_claims, authority_claims=claims)
            session = HttpSession(secrets.token_urlsafe(32), is_new=True)
            session.rotate_id(on_login=True)
            cookie = session.id + secrets.token_urlsafe(32)
            async with self.store._unit():
                await self.sessions.save(
                    ForgeIdentitySessionEntity(
                        user_id=user.id,
                        cookie_hash=digest(cookie),
                        status="active",
                        revision=1,
                        expires_at=datetime.now(UTC) + timedelta(seconds=self.config.session_seconds),
                        access_expires_at=datetime.fromtimestamp(claims["exp"], UTC),
                        encrypted_data=self._encrypt(tokens),
                    )
                )
            return LoginSession(cookie, self._principal(user, claims, "oidc_session"), self.config.session_seconds)
        except Exception:
            raise ValueError("OIDC login failed; begin a new login") from None

    async def authenticate(self, request: Request) -> Principal | None:
        authorization = request.headers.get("authorization", "")
        if len(authorization) > 16391:
            return None
        if self.config.mode == "local_bootstrap":
            if (
                request.client
                and request.client.host in {"127.0.0.1", "::1"}
                and hmac.compare_digest(
                    authorization.encode(), ("Bearer " + self.config.bootstrap_token.get_secret_value()).encode()
                )
            ):
                try:
                    bootstrap_user = await self._bootstrap_user()
                except ValueError:
                    return None
                return Principal(
                    bootstrap_user.id,
                    bootstrap_user.issuer,
                    bootstrap_user.subject,
                    authentication_method="local_bootstrap",
                    kind="local_bootstrap",
                )
            if authorization:
                return None
            cookie = request.cookies.get(SESSION_COOKIE, "")
            if (
                request.client is None
                or request.client.host not in {"127.0.0.1", "::1"}
                or not cookie
                or len(cookie) > 200
            ):
                return None
            async with self.store._unit():
                row = await self.sessions.cookie(digest(cookie))
                if row is None or row.status != "active" or row.expires_at <= datetime.now(UTC):
                    return None
                user = await self.users.find_by_id(row.user_id)
                if user is None or not user.enabled:
                    return None
                values = self._decrypt(row.encrypted_data)
                if not hmac.compare_digest(
                    values.get("bootstrap_token_hash", ""), digest(self.config.bootstrap_token.get_secret_value())
                ):
                    return None
            return Principal(
                user.id, user.issuer, user.subject, authentication_method="local_bootstrap", kind="local_bootstrap"
            )
        try:
            if authorization:
                if authorization.startswith("Bearer forge_pat_"):
                    if self.personal_token_manager is None:
                        return None
                    authenticated = await self.personal_token_manager.authenticate(
                        Authentication(
                            principal="personal-token",
                            credentials=authorization[7:],
                            details={"credential_kind": "personal_token"},
                        )
                    )
                    return Principal(
                        UUID(authenticated.principal),
                        authenticated.details["issuer"],
                        authenticated.details["subject"],
                        authenticated.details.get("display_name"),
                        authenticated.details.get("email"),
                        authentication_method="personal_token",
                        kind="service",
                        personal_token_id=UUID(authenticated.details["personal_token_id"]),
                    )
                if not authorization.startswith("Bearer ") or len(authorization) > 16391:
                    return None
                claims = await self._access(authorization[7:])
                return self._principal(await self._user(claims), claims, "oidc_bearer")
            cookie = request.cookies.get(SESSION_COOKIE, "")
            if not cookie or len(cookie) > 200:
                return None
            async with self.store._unit():
                row = await self.sessions.cookie(digest(cookie))
                if row is None or row.status != "active" or row.expires_at <= datetime.now(UTC):
                    return None
                tokens = self._decrypt(row.encrypted_data)
                expired = row.access_expires_at <= datetime.now(UTC) + timedelta(seconds=15)
                if expired:
                    row.status, row.revision = "refreshing", row.revision + 1
                    await self.sessions.save(row)
                revision = row.revision
            if expired:
                refreshed = await self._refresh(cookie, revision, tokens)
                if not refreshed:
                    return None
                tokens = refreshed
            claims = await self._access(tokens["access_token"])
            user = await self._user(claims)
            async with self.store._unit():
                current = await self.sessions.cookie(digest(cookie))
                if current is None or current.status != "active" or current.expires_at <= datetime.now(UTC):
                    return None
            return self._principal(user, claims, "oidc_session")
        except Exception:
            return None

    async def _refresh(self, cookie: str, revision: int, tokens: dict[str, Any]) -> dict[str, Any] | None:
        assert self.registration is not None
        try:
            refreshed = await self._json(
                "POST",
                self.registration.token_uri,
                form={
                    "grant_type": "refresh_token",
                    "refresh_token": tokens["refresh_token"],
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret.get_secret_value(),
                },
            )
            claims = await self._access(refreshed["access_token"])
            async with self.store._unit():
                row = await self.sessions.cookie(digest(cookie))
                if row is None or row.status != "refreshing" or row.revision != revision:
                    return None
                user = await self.users.find_by_id(row.user_id)
                if user is None or not user.enabled or user.subject != claims["sub"]:
                    row.status = "revoked"
                    await self.sessions.save(row)
                    return None
            await self._user(claims)
            async with self.store._unit():
                row = await self.sessions.cookie(digest(cookie))
                if row is None or row.status != "refreshing" or row.revision != revision:
                    return None
                user = await self.users.find_by_id(row.user_id)
                if user is None or not user.enabled or user.subject != claims["sub"]:
                    row.status = "revoked"
                    await self.sessions.save(row)
                    return None
                refreshed.setdefault("refresh_token", tokens.get("refresh_token"))
                row.encrypted_data, row.status = self._encrypt(refreshed), "active"
                row.access_expires_at = datetime.fromtimestamp(claims["exp"], UTC)
                await self.sessions.save(row)
            return refreshed
        except Exception:
            async with self.store._unit():
                row = await self.sessions.cookie(digest(cookie))
                if row and row.status == "refreshing" and row.revision == revision:
                    row.status = "revoked"
                    await self.sessions.save(row)
            return None

    async def logout(self, cookie: str) -> None:
        if not cookie or len(cookie) > 200:
            return
        tokens = None
        async with self.store._unit():
            row = await self.sessions.cookie(digest(cookie))
            if row:
                tokens = self._decrypt(row.encrypted_data)
                row.status, row.revision, row.encrypted_data = "revoked", row.revision + 1, self._encrypt({})
                await self.sessions.save(row)
        if tokens and tokens.get("refresh_token") and self.registration:
            try:
                async with self.client.stream(
                    "POST",
                    self.config.issuer + "/protocol/openid-connect/revoke",
                    data={
                        "token": tokens["refresh_token"],
                        "token_type_hint": "refresh_token",
                        "client_id": self.config.client_id,
                        "client_secret": self.config.client_secret.get_secret_value(),
                    },
                    timeout=10,
                    follow_redirects=False,
                ):
                    pass
            except httpx.HTTPError:
                pass

    def check_cookie_origin(self, request: Request) -> None:
        expected = urlsplit(self.config.redirect_uri) if self.config.mode == "oidc" else urlsplit(str(request.url))
        origin = request.headers.get("origin")
        if origin != (self.config.browser_origin or f"{expected.scheme}://{expected.netloc}"):
            raise ValueError("Cookie-authenticated mutations require the configured application origin")
