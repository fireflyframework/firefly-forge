# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Project observation credentials never inherit operator or execution authority."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.requests import Request

from firefly_forge.access_routes import AccessRoutes
from firefly_forge.identity import Principal
from firefly_forge.project_access import AccessDenied, ProjectAccess


def token_principal(*, roles=frozenset()):
    return Principal(
        uuid4(),
        "https://identity.example/realms/forge",
        "observer",
        authentication_method="personal_token",
        api_roles=roles,
    )


def test_personal_credentials_cannot_inherit_factory_admin_even_from_owner_roles():
    assert not ProjectAccess.is_admin(token_principal(roles=frozenset({"forge-admin"})))


async def test_personal_credentials_cannot_invoke_even_pure_post_previews():
    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    request = Request(
        {"type": "http", "method": "POST", "scheme": "https", "path": "/api/v1/architecture/propose", "headers": []},
        receive=receive,
    )
    with pytest.raises(AccessDenied):
        await AccessRoutes(ProjectAccess(SimpleNamespace()), SimpleNamespace()).authorize(token_principal(), request)


@pytest.fixture
async def token_store(tmp_path):
    from contextlib import asynccontextmanager

    from pyfly.data.relational.sqlalchemy import SessionProvider, SqlAlchemyTransactionManager
    from pyfly.data.transaction.registry import TransactionManagerRegistry
    from sqlalchemy import event
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.ext.compiler import compiles

    from firefly_forge.access_persistence import ForgeMembershipEntity, ForgeMembershipRepository
    from firefly_forge.identity_persistence import ForgeIdentityUserEntity, ForgeIdentityUserRepository
    from firefly_forge.personal_token_persistence import ForgePersonalTokenEntity, ForgePersonalTokenEventEntity
    from firefly_forge.personal_tokens import PersonalTokens
    from firefly_forge.project_persistence import ForgeProjectEntity, ForgeProjectRepository
    from firefly_forge.secrets import contains_known_credential

    @compiles(JSONB, "sqlite")
    def json_column(element, compiler, **kw):
        return "JSON"

    database = tmp_path / "personal-token-host.sqlite"
    engine = create_async_engine("sqlite+aiosqlite:///" + str(database))

    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    entities = [
        ForgeIdentityUserEntity,
        ForgeProjectEntity,
        ForgeMembershipEntity,
        ForgePersonalTokenEntity,
        ForgePersonalTokenEventEntity,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: ForgePersonalTokenEntity.metadata.create_all(
                sync, tables=[entity.__table__ for entity in entities]
            )
        )
    registry = TransactionManagerRegistry()
    registry.register(SqlAlchemyTransactionManager(sessionmaker=async_sessionmaker(engine, expire_on_commit=False)))
    sessions = SessionProvider(registry)

    class DataStore:
        schema = "host_sqlite"

        @asynccontextmanager
        async def _unit(self):
            async with sessions.unit() as session:
                yield session

        def reject_credentials(self, value):
            if contains_known_credential(value):
                raise ValueError("Credential material cannot be retained")

    store = DataStore()
    owner = Principal(uuid4(), "https://identity.example/realms/forge", "owner", authentication_method="oidc_session")
    foreign = Principal(uuid4(), owner.issuer, "other-owner", authentication_method="oidc_session")
    project, other = uuid4(), uuid4()
    async with store._unit():
        for user in [owner, foreign]:
            await ForgeIdentityUserRepository().save(
                ForgeIdentityUserEntity(
                    id=user.id, issuer=user.issuer, subject=user.subject, enabled=True, api_roles=[]
                )
            )
        for project_id in [project, other]:
            await ForgeProjectRepository().save(ForgeProjectEntity(id=project_id, data={"name": "Controlled project"}))
        await ForgeMembershipRepository().save(
            ForgeMembershipEntity(project_id=project, user_id=owner.id, role="owner", active=True, revision=1)
        )
    value = SimpleNamespace(
        store=store,
        owner=owner,
        foreign=foreign,
        project=project,
        other=other,
        service=PersonalTokens(store),
        engine=engine,
    )
    store.personal_tokens = value.service
    try:
        yield value
    finally:
        await engine.dispose()


def token_input(context, **changes):
    from datetime import UTC, datetime, timedelta

    from firefly_forge.personal_token_models import PersonalTokenInput

    return PersonalTokenInput(
        **{
            "request_id": uuid4().hex,
            "name": "Project observer",
            "project_ids": [context.project.hex],
            "expires_at": datetime.now(UTC) + timedelta(days=1),
            **changes,
        }
    )


async def test_issue_discloses_once_and_persists_only_digest_through_native_repositories(token_store):
    import hashlib
    import json

    from sqlalchemy import select

    from firefly_forge.personal_token_persistence import ForgePersonalTokenEntity, ForgePersonalTokenEventEntity

    c = token_store
    request = token_input(c)
    issued = await c.service.create(c.owner, request)
    raw = issued.secret.get_secret_value()
    assert len(raw.split(".")[1]) == 43
    assert raw not in repr(issued)
    repeated = await c.service.create(c.owner, request)
    assert repeated.secret is None and repeated.disclose()["secret_state"] == "not_retained"
    assert repeated.credential["id"] == issued.credential["id"]
    async with c.store._unit() as session:
        rows = list(await session.scalars(select(ForgePersonalTokenEntity)))
        events = list(await session.scalars(select(ForgePersonalTokenEventEntity)))
    assert len(rows) == len(events) == 1
    assert rows[0].secret_digest == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in json.dumps(events[0].data)
    assert raw not in json.dumps(await c.service.page(c.owner))
    assert "secret_digest" not in json.dumps(await c.service.page(c.owner))
    with pytest.raises(ValueError):
        await c.service.create(c.owner, request.model_copy(update={"name": "Different request"}))


async def test_rotation_revokes_old_credential_once_and_cannot_reveal_replayed_secret(token_store):
    from datetime import UTC, datetime, timedelta

    from firefly_forge.personal_token_models import PersonalTokenRotationInput

    c = token_store
    first = await c.service.create(c.owner, token_input(c))
    request = PersonalTokenRotationInput(request_id=uuid4().hex, expires_at=datetime.now(UTC) + timedelta(days=2))
    rotated = await c.service.rotate(c.owner, first.credential["id"], request)
    assert rotated.credential["predecessor_id"] == first.credential["id"]
    with pytest.raises(AccessDenied):
        await c.service.resolve(first.secret.get_secret_value())
    grant = await c.service.resolve(rotated.secret.get_secret_value())
    assert grant.principal.authentication_method == "personal_token"
    assert grant.principal.kind == "service" and not grant.principal.api_roles
    assert grant.memberships[0]["permissions"] == ["read"]
    replayed = await c.service.rotate(c.owner, first.credential["id"], request)
    assert replayed.secret is None and replayed.credential["id"] == rotated.credential["id"]
    revoked = await c.service.revoke(c.owner, rotated.credential["id"])
    assert revoked["status"] == "revoked"
    assert (await c.service.revoke(c.owner, rotated.credential["id"])) == revoked
    with pytest.raises(AccessDenied):
        await c.service.resolve(rotated.secret.get_secret_value())


@pytest.mark.parametrize("change", ["disabled", "membership_removed", "expired", "wrong_secret"])
async def test_current_native_identity_membership_expiry_and_digest_are_required(token_store, change):
    from datetime import UTC, datetime, timedelta
    from uuid import UUID

    from firefly_forge.access_persistence import ForgeMembershipRepository
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.personal_token_persistence import ForgePersonalTokenRepository

    c = token_store
    issued = await c.service.create(c.owner, token_input(c))
    raw = issued.secret.get_secret_value()
    async with c.store._unit():
        if change == "disabled":
            user = await ForgeIdentityUserRepository().find_by_id(c.owner.id)
            user.enabled = False
            await ForgeIdentityUserRepository().save(user)
        elif change == "membership_removed":
            membership = (await ForgeMembershipRepository().for_user(c.owner.id))[0]
            membership.active = False
            await ForgeMembershipRepository().save(membership)
        elif change == "expired":
            row = await ForgePersonalTokenRepository().find_by_id(UUID(hex=issued.credential["id"]))
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await ForgePersonalTokenRepository().save(row)
        else:
            raw = raw[:-1] + ("A" if raw[-1] != "A" else "B")
    with pytest.raises(AccessDenied):
        await c.service.resolve(raw)


async def test_interactive_owner_and_explicit_current_project_memberships_bound_issuance(token_store):
    from dataclasses import replace

    c = token_store
    for method in ["oidc_bearer", "local_bootstrap", "scm_mapping", "personal_token"]:
        with pytest.raises(AccessDenied):
            await c.service.create(replace(c.owner, authentication_method=method), token_input(c))
    with pytest.raises(AccessDenied):
        await c.service.create(c.owner, token_input(c, project_ids=[c.other.hex]))
    issued = await c.service.create(c.owner, token_input(c))
    with pytest.raises(KeyError):
        await c.service.revoke(c.foreign, issued.credential["id"])


async def test_failed_native_transaction_does_not_leave_a_credential_or_rotate_the_predecessor(
    token_store, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from firefly_forge.personal_token_models import PersonalTokenRotationInput

    c = token_store
    first = await c.service.create(c.owner, token_input(c))

    async def refuse_audit(row):
        raise ValueError("Controlled audit failure")

    monkeypatch.setattr(c.service.events, "save", refuse_audit)
    with pytest.raises(ValueError, match="audit failure"):
        await c.service.rotate(
            c.owner,
            first.credential["id"],
            PersonalTokenRotationInput(request_id=uuid4().hex, expires_at=datetime.now(UTC) + timedelta(days=2)),
        )
    assert (await c.service.resolve(first.secret.get_secret_value())).principal.id == c.owner.id
    assert len((await c.service.page(c.owner))["items"]) == 1


async def test_native_authentication_spi_accepts_pat_origin_and_rejects_revocation_without_oidc_fallback(token_store):
    import httpx
    from cryptography.fernet import Fernet

    from firefly_forge.identity import IdentityConfiguration, IdentityService
    from firefly_forge.personal_tokens import PersonalTokenAuthenticationProvider

    c = token_store
    issued = await c.service.create(c.owner, token_input(c))
    requests = []

    def forbidden(request):
        requests.append(request.url.path)
        raise AssertionError("Personal credentials do not make an OIDC network request")

    client = httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
    config = IdentityConfiguration(
        issuer=c.owner.issuer,
        client_id="forge-ui",
        api_audience="forge-api",
        redirect_uri="https://forge.example/api/v1/auth/callback",
        encryption_key=Fernet.generate_key().decode(),
    )
    identity = IdentityService(
        c.store, config=config, client=client, personal_token_provider=PersonalTokenAuthenticationProvider(c.service)
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": "/api/v1/projects",
            "headers": [(b"authorization", ("Bearer " + issued.secret.get_secret_value()).encode())],
        }
    )
    try:
        user = await identity.authenticate(request)
        assert user.id == c.owner.id and user.authentication_method == "personal_token"
        assert user.kind == "service" and not user.api_roles
        acl = ProjectAccess(c.store)
        await acl.require_project(user, c.project.hex, "read")
        for action in ["edit", "execute", "review", "release", "members"]:
            with pytest.raises(AccessDenied):
                await acl.require_project(user, c.project.hex, action)
        actor = acl.actor_snapshot(user)
        assert actor["authentication_method"] == "personal_token" and actor["global_admin"] is False
        with pytest.raises(AccessDenied):
            await acl.authorize_actor(actor, c.project.hex, "execute")
        with pytest.raises(AccessDenied):
            await acl.require_project(user, c.other.hex, "read")
        await c.service.revoke(c.owner, issued.credential["id"])
        assert await identity.authenticate(request) is None
        with pytest.raises(AccessDenied):
            await acl.memberships(user)
        assert requests == []
    finally:
        await client.aclose()


def test_personal_format_is_redacted_and_rejected_across_activity_chunk_boundaries():
    from firefly_forge.secrets import KnownCredentialScan, contains_known_credential, redact_environment

    raw = "forge_pat_" + "1" * 32 + "." + "Q" * 43
    assert contains_known_credential({"output": "private " + raw})
    assert raw not in redact_environment("private " + raw)
    for boundary in [10, 35, 60, 85]:
        scanner = KnownCredentialScan(())
        assert scanner.feed(raw[:boundary]) or scanner.feed(raw[boundary:]) or scanner.feed(None)


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/providers/bridge/configuration"),
        ("GET", "/api/v1/identity/tokens"),
        ("GET", "/api/v1/identity/users"),
        ("GET", "/api/v1/oauth/provider-policies"),
        ("GET", "/api/v1/projects/" + "a" * 32 + "/members"),
        ("POST", "/api/v1/knowledge/search"),
        ("POST", "/api/v1/runs"),
        ("POST", "/mcp"),
    ],
)
async def test_observation_route_policy_denies_writes_global_configuration_and_credentials(method, path):
    request = Request({"type": "http", "method": method, "scheme": "https", "path": path, "headers": []})
    with pytest.raises(AccessDenied):
        await AccessRoutes(ProjectAccess(SimpleNamespace()), SimpleNamespace()).authorize(token_principal(), request)


@pytest.mark.parametrize("field", ["issuer", "subject"])
async def test_persisted_credentials_cannot_transfer_when_an_identity_record_is_rebound(token_store, field):
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository

    c = token_store
    issued = await c.service.create(c.owner, token_input(c))
    async with c.store._unit():
        user = await ForgeIdentityUserRepository().find_by_id(c.owner.id)
        setattr(user, field, "replacement-identity")
        await ForgeIdentityUserRepository().save(user)
    with pytest.raises(AccessDenied):
        await c.service.resolve(issued.secret.get_secret_value())


async def test_credential_remains_usable_after_native_connection_pool_restart_without_reissuing(token_store):
    from firefly_forge.personal_tokens import PersonalTokens

    c = token_store
    issued = await c.service.create(c.owner, token_input(c))
    await c.engine.dispose()
    restarted = PersonalTokens(c.store)
    assert (await restarted.resolve(issued.secret.get_secret_value())).principal.id == c.owner.id
    assert (await restarted.page(c.owner))["items"][0]["id"] == issued.credential["id"]


@pytest.fixture
async def token_http(token_store, monkeypatch):
    import httpx
    from cryptography.fernet import Fernet
    from pyfly.container import Container
    from pyfly.web.adapters.starlette.controller import ControllerRegistrar
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    from firefly_forge.api import OperatorAuth
    from firefly_forge.controllers import OperatorErrors, RunController
    from firefly_forge.identity import SESSION_COOKIE, IdentityConfiguration, IdentityService
    from firefly_forge.personal_token_controllers import PersonalTokenController
    from firefly_forge.personal_tokens import PersonalTokenAuthenticationProvider, PersonalTokens

    c = token_store

    def refuse_oidc(_request):
        raise AssertionError("The controlled token transport must not call OIDC")

    model_http = httpx.AsyncClient(transport=httpx.MockTransport(refuse_oidc))
    identity = IdentityService(
        c.store,
        config=IdentityConfiguration(
            issuer=c.owner.issuer,
            client_id="forge-ui",
            api_audience="forge-api",
            redirect_uri="https://forge.example/api/v1/auth/callback",
            encryption_key=Fernet.generate_key().decode(),
        ),
        client=model_http,
        personal_token_provider=PersonalTokenAuthenticationProvider(c.service),
    )
    original = identity.authenticate

    async def controlled_session(request):
        if not request.headers.get("authorization"):
            if request.cookies.get(SESSION_COOKIE) == "controlled-owner-session":
                return c.owner
            if request.cookies.get(SESSION_COOKIE) == "controlled-foreign-session":
                return c.foreign
        return await original(request)

    monkeypatch.setattr(identity, "authenticate", controlled_session)
    c.run_id, c.foreign_run = uuid4().hex, uuid4().hex
    run_rows = [
        {"id": c.run_id, "project_id": c.project.hex, "status": "succeeded", "events": []},
        {"id": c.foreign_run, "project_id": c.other.hex, "status": "succeeded", "events": []},
    ]

    async def get_run(value):
        return next(row.copy() for row in run_rows if row["id"] == value)

    async def runs():
        return [row.copy() for row in run_rows]

    c.store.get_run, c.store.runs = get_run, runs
    acl = ProjectAccess(c.store)
    container = Container()
    container.register_instance(PersonalTokens, c.service)
    container.register(PersonalTokenController)
    container.register_instance(OperatorErrors, OperatorErrors())
    container.register_instance(RunController, RunController(SimpleNamespace(), c.store, SimpleNamespace(), acl))
    context = SimpleNamespace(container=container, get_bean=container.resolve)
    app = Starlette(
        routes=ControllerRegistrar().collect_routes(context),
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=OperatorAuth("", identity, acl, c.store).do_filter)],
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://forge.example"
        ) as client:
            yield SimpleNamespace(context=c, client=client)
    finally:
        await model_http.aclose()


async def test_native_web_controller_owner_disclosure_scoped_observation_and_revocation(token_http):
    c, client = token_http.context, token_http.client
    owner = {"Cookie": "forge_session=controlled-owner-session", "Origin": "https://forge.example"}
    request = token_input(c).model_dump(mode="json")
    created = await client.post("/api/v1/identity/tokens", json=request, headers=owner)
    assert created.status_code == 201, created.text
    assert "no-store" in created.headers["cache-control"]
    body = created.json()
    assert body["secret_state"] == "returned_once"
    bearer = {"Authorization": "Bearer " + body["token"]}
    assert (await client.get("/api/v1/runs/" + c.run_id, headers=bearer)).status_code == 200
    assert (await client.get("/api/v1/runs/" + c.foreign_run, headers=bearer)).status_code == 403
    assert [row["id"] for row in (await client.get("/api/v1/runs", headers=bearer)).json()] == [c.run_id]
    for path in ["/api/v1/identity/tokens", "/api/v1/providers/bridge/configuration"]:
        assert (await client.get(path, headers=bearer)).status_code == 403
    assert (await client.post("/api/v1/runs", json={}, headers=bearer)).status_code == 403
    repeated = await client.post("/api/v1/identity/tokens", json=request, headers=owner)
    assert repeated.status_code == 201 and "token" not in repeated.json()
    listed = await client.get("/api/v1/identity/tokens", headers=owner)
    assert body["token"] not in listed.text and "secret_digest" not in listed.text
    revoked = await client.post("/api/v1/identity/tokens/" + body["credential"]["id"] + "/revoke", headers=owner)
    assert revoked.status_code == 200
    assert (await client.get("/api/v1/runs/" + c.run_id, headers=bearer)).status_code == 401


async def test_native_web_controller_requires_same_origin_and_rejects_expanded_permissions(token_http):
    c, client = token_http.context, token_http.client
    request = token_input(c).model_dump(mode="json")
    assert (await client.post("/api/v1/identity/tokens", json=request)).status_code == 401
    owner = {"Cookie": "forge_session=controlled-owner-session", "Origin": "https://attacker.example"}
    assert (await client.post("/api/v1/identity/tokens", json=request, headers=owner)).status_code == 403
    owner["Origin"] = "https://forge.example"
    request["permissions"] = ["execute"]
    invalid = await client.post("/api/v1/identity/tokens", json=request, headers=owner)
    assert invalid.status_code == 422
    assert (await client.get("/api/v1/identity/tokens", headers=owner)).json()["items"] == []


async def test_paginated_owner_metadata_has_no_fixed_total_limit_or_repeated_secret(token_store):
    c = token_store
    ids = set()
    for _ in range(4):
        value = await c.service.create(c.owner, token_input(c))
        ids.add(value.credential["id"])
    seen, after = [], None
    while True:
        page = await c.service.page(c.owner, after=after, limit=1)
        seen.extend(row["id"] for row in page["items"])
        after = page["next_cursor"]
        if after is None:
            break
    assert len(seen) == len(set(seen)) == 4 and set(seen) == ids
    assert (await c.service.page(c.foreign))["items"] == []


async def test_native_provider_erases_submitted_secret_and_emits_no_admin_roles(token_store):
    from pyfly.security.authentication import Authentication, ProviderManager

    from firefly_forge.personal_tokens import PersonalTokenAuthenticationProvider

    c = token_store
    issued = await c.service.create(c.owner, token_input(c))
    request = Authentication(
        principal="personal-token",
        credentials=issued.secret.get_secret_value(),
        details={"credential_kind": "personal_token"},
    )
    result = await ProviderManager(PersonalTokenAuthenticationProvider(c.service)).authenticate(request)
    assert result.authenticated and result.roles == [] and result.permissions == ["read"]
    assert request.credentials is None and result.credentials is None
    assert issued.secret.get_secret_value() not in repr(result)


async def test_python_sdk_observation_uses_the_same_native_authentication_and_route_denials(token_http):
    import anyio
    import httpx

    from firefly_forge.client import ForgeClient

    c, native = token_http.context, token_http.client
    created = await native.post(
        "/api/v1/identity/tokens",
        json=token_input(c).model_dump(mode="json"),
        headers={"Cookie": "forge_session=controlled-owner-session", "Origin": "https://forge.example"},
    )
    raw = created.json()["token"]

    async def dispatch(request):
        response = await native.request(
            request.method, request.url.path, headers=request.headers, content=request.content
        )
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    def transport(request):
        return anyio.from_thread.run(dispatch, request)

    def observe():
        with ForgeClient("https://forge.example", raw) as client:
            client.http.close()
            client.http = httpx.Client(
                base_url="https://forge.example/api/v1/",
                headers={"Authorization": "Bearer " + raw},
                transport=httpx.MockTransport(transport),
            )
            assert [row["id"] for row in client.request("GET", "runs")] == [c.run_id]
            with pytest.raises(RuntimeError, match="HTTP 403"):
                client.request("POST", "runs", json={})
            with pytest.raises(RuntimeError, match="HTTP 403"):
                client.request("GET", "providers/bridge/configuration")

    await anyio.to_thread.run_sync(observe)


def test_default_openapi_documents_personal_credential_lifecycle_without_secret_read_routes():
    from firefly_forge.api import openapi_document

    document = openapi_document()
    base = document["paths"]["/api/v1/identity/tokens"]
    assert set(base) == {"get", "post"}
    assert base["post"]["security"] == [{"browserSession": []}]
    assert document["components"]["securitySchemes"]["browserSession"]["name"] == "forge_session"
    schema = document["components"]["schemas"]["PersonalTokenInput"]
    assert schema["properties"]["permissions"]["items"]["const"] == "read"
    assert document["paths"]["/api/v1/identity/tokens/{token_id}/revoke"]["post"]
    assert document["paths"]["/api/v1/identity/tokens/{token_id}/rotate"]["post"]


async def test_production_configuration_beans_share_native_personal_authority(token_store, tmp_path):
    from cryptography.fernet import Fernet

    from firefly_forge.access_persistence import ForgeMembershipRepository
    from firefly_forge.api import FactoryConfiguration
    from firefly_forge.identity import IdentityConfiguration
    from firefly_forge.identity_persistence import (
        ForgeIdentityAttemptRepository,
        ForgeIdentitySessionRepository,
        ForgeIdentityUserRepository,
    )
    from firefly_forge.personal_token_persistence import ForgePersonalTokenEventRepository, ForgePersonalTokenRepository

    c = token_store
    configuration = FactoryConfiguration(
        tmp_path,
        "",
        IdentityConfiguration(
            issuer=c.owner.issuer,
            client_id="forge-ui",
            api_audience="forge-api",
            redirect_uri="https://forge.example/api/v1/auth/callback",
            encryption_key=Fernet.generate_key().decode(),
        ),
    )
    users, memberships = ForgeIdentityUserRepository(), ForgeMembershipRepository()
    tokens = configuration.personal_tokens(
        c.store, ForgePersonalTokenRepository(), ForgePersonalTokenEventRepository(), users, memberships
    )
    access = configuration.project_access(c.store, memberships, tokens)
    identity = configuration.identity_service(
        c.store, users, ForgeIdentitySessionRepository(), ForgeIdentityAttemptRepository(), tokens
    )
    try:
        raw = (await tokens.create(c.owner, token_input(c))).secret.get_secret_value()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "path": "/api/v1/runs",
                "headers": [(b"authorization", ("Bearer " + raw).encode())],
            }
        )
        principal = await identity.authenticate(request)
        assert principal.authentication_method == "personal_token"
        assert c.store.personal_tokens is tokens is access.personal_tokens
        await access.require_project(principal, c.project.hex, "read")
        with pytest.raises(AccessDenied):
            await access.require_project(principal, c.project.hex, "execute")
    finally:
        await identity.stop()
