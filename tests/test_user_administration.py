# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import importlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError


def module():
    return importlib.import_module("firefly_forge.user_administration")


async def setup(store, *, outcome="created", invite_status=204, broad=False, composite=False):
    from firefly_forge.identity import IdentityConfiguration, IdentityService, Principal
    from firefly_forge.identity_persistence import ForgeIdentityUserEntity

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="admin-key", alg="RS256", use="sig")
    config = IdentityConfiguration(
        mode="oidc",
        issuer="https://id.example/realms/forge",
        client_id="studio",
        client_secret="studio-client-secret",
        api_audience="forge-api",
        redirect_uri="https://forge.example/api/v1/auth/callback",
        encryption_key=Fernet.generate_key().decode(),
    )
    identity = IdentityService(store, config=config)
    actor = Principal(uuid.uuid4(), config.issuer, "admin-actor", api_roles=frozenset({"forge-admin"}))
    target, calls, remote = uuid.uuid4(), [], {}
    async with store._unit():
        await identity.users.save(
            ForgeIdentityUserEntity(id=actor.id, issuer=config.issuer, subject=actor.subject, enabled=True)
        )

    async def transport(req):
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/certs"):
            return httpx.Response(200, json={"keys": [jwk]})
        if req.url.path.endswith("/token"):
            claims = {
                "iss": config.issuer,
                "sub": "service-identity",
                "aud": "realm-management",
                "azp": "forge-user-admin",
                "exp": int(time.time()) + 300,
                "iat": int(time.time()),
                "typ": "Bearer",
                "resource_access": {
                    "realm-management": {
                        "roles": ["view-users", "manage-users"]
                        + (["realm-admin"] if broad else [])
                        + (["query-users", "query-groups"] if composite else [])
                    }
                },
            }
            return httpx.Response(
                200, json={"access_token": jwt.encode(claims, key, algorithm="RS256", headers={"kid": "admin-key"})}
            )
        if req.method == "POST":
            from firefly_forge.user_administration_persistence import ForgeIdentityAdminOperationRepository

            async with store._unit():
                rows = await ForgeIdentityAdminOperationRepository().latest()
                assert rows and rows[0].status == "executing"
            remote.update(json.loads(req.content), id=str(target))
            if outcome == "uncertain":
                raise httpx.ReadTimeout("private provider payload unavailable", request=req)
            return httpx.Response(
                201,
                headers={"location": config.issuer.split("/realms/")[0] + "/admin/realms/forge/users/" + str(target)},
            )
        if req.method == "PUT":
            if invite_status == "uncertain":
                raise httpx.ReadTimeout("unknown mail admission", request=req)
            assert json.loads(req.content) == ["UPDATE_PASSWORD", "VERIFY_EMAIL"]
            return httpx.Response(invite_status)
        return httpx.Response(200, json=remote)

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    identity.client = client
    service = module().UserAdministration(
        store,
        identity,
        config=module().KeycloakAdministrationConfiguration(
            client_id="forge-user-admin", client_secret="private-admin-client-secret"
        ),
        client=client,
    )
    from test_identity import request

    issued_at = int(time.time())
    credential = jwt.encode(
        {
            "iss": config.issuer,
            "sub": actor.subject,
            "aud": config.api_audience,
            "iat": issued_at,
            "exp": issued_at + 300,
            "typ": "Bearer",
            "resource_access": {config.api_audience: {"roles": ["forge-admin"]}},
        },
        key,
        algorithm="RS256",
        headers={"kid": "admin-key"},
    )
    actor = await identity.authenticate(request(bearer="Bearer " + credential))
    assert actor is not None
    calls.clear()
    return service, actor, target, calls, remote


async def test_suspend_revokes_all_sessions_atomically_and_self_or_bootstrap_is_protected(store):
    from firefly_forge.identity_persistence import ForgeIdentitySessionEntity, ForgeIdentityUserEntity

    service, actor, _, _, _ = await setup(store)
    user = ForgeIdentityUserEntity(
        id=uuid.uuid4(), issuer=service.identity.config.issuer, subject="another-user", enabled=True
    )
    async with store._unit():
        await service.identity.users.save(user)
        for index in range(3):
            await service.identity.sessions.save(
                ForgeIdentitySessionEntity(
                    user_id=user.id,
                    cookie_hash=str(index) * 64,
                    status="active",
                    revision=1,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                    access_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                    encrypted_data=service.identity._encrypt({"private": "value"}),
                )
            )
    result = await service.set_enabled(user.id.hex, False, actor)
    assert result["enabled"] is False and result["revoked_sessions"] == 3
    async with store._unit():
        assert (await service.identity.users.find_by_id(user.id)).enabled is False
    with pytest.raises(ValueError, match="yourself"):
        await service.set_enabled(actor.id.hex, False, actor)
    bootstrap = ForgeIdentityUserEntity(id=uuid.uuid4(), issuer="local:bootstrap", subject="operator", enabled=True)
    async with store._unit():
        await service.identity.users.save(bootstrap)
    with pytest.raises(ValueError, match="bootstrap"):
        await service.set_enabled(bootstrap.id.hex, False, actor)


async def test_provision_has_durable_intent_and_reuses_receipt_without_granting_projects(store):
    service, actor, target, calls, _ = await setup(store)
    request = module().UserProvisionInput(request_id=uuid.uuid4().hex, username="new-owner", email="owner@example.test")
    result = await service.provision(request, actor)
    assert result["status"] == "completed" and result["subject"] == str(target)
    count = sum(method == "POST" and path.endswith("/users") for method, path in calls)
    assert await service.provision(request, actor) == result
    assert sum(method == "POST" and path.endswith("/users") for method, path in calls) == count == 1
    assert result["project_memberships_granted"] is False and result["invitation_status"] == "not_requested"
    assert (await service.receipts())[0]["journal_valid"] is True
    assert "private-admin-client-secret" not in json.dumps(result)
    with pytest.raises(ValueError, match="replay"):
        await service.provision(request.model_copy(update={"username": "different-owner"}), actor)


async def test_provisioned_contact_is_visible_inviteable_and_not_verified_authority(store):
    from firefly_forge.access_controllers import AccessController
    from firefly_forge.identity import Principal
    from firefly_forge.models import RepositoryInput
    from firefly_forge.project_access import ProjectAccess
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects

    service, actor, _, _, _ = await setup(store)
    created = await service.provision(
        module().UserProvisionInput(request_id=uuid.uuid4().hex, username="new-owner", email="owner@example.test"),
        actor,
    )
    user_id = uuid.UUID(created["user_id"])
    async with store._unit():
        user = await service.identity.users.find_by_id(user_id)
        assert user.display_name == "new-owner"
        assert user.contact_email == "owner@example.test"
        assert user.email is None
        principal = Principal(user.id, user.issuer, user.subject)
    access = ProjectAccess(store)
    assert await access.memberships(principal) == []
    controller = AccessController(access, store)
    listed = next(row for row in await controller.users() if row["id"] == user_id.hex)
    assert listed["email"] == listed["contact_email"] == "owner@example.test"
    assert listed["email_verified"] is False
    repo = await store.add_repository(
        RepositoryInput(name="Contact project", url="https://github.com/team/service.git")
    )
    project = await Projects(store).configure(
        ProjectDefinition(
            name="Directory membership",
            description="Explicitly assigned contacts",
            topology="monorepo",
            owners=["Platform"],
            components=[dict(id="service", name="Service", repository_id=repo["id"], responsibility="Service")],
        )
    )
    assert [row["id"] for row in await controller.eligible(project["id"], query="owner@example.test")] == [user_id.hex]
    await access.set_membership(project["id"], principal, "viewer")
    member = (await controller.members(project["id"]))[0]
    assert member["email"] == member["contact_email"] == "owner@example.test"
    assert member["email_verified"] is False and member["role"] == "viewer"
    invited = await service.invite(user_id.hex, module().UserInviteInput(request_id=uuid.uuid4().hex), actor)
    assert invited["invitation_status"] == "provider_accepted"
    assert invited["mail_delivery_verified"] is False


async def test_unverified_reauthentication_preserves_contact_without_verified_claim_or_grant(store):
    from firefly_forge.project_access import ProjectAccess

    service, actor, target, _, _ = await setup(store)
    created = await service.provision(
        module().UserProvisionInput(request_id=uuid.uuid4().hex, username="new-owner", email="owner@example.test"),
        actor,
    )
    claims = {
        "sub": str(target),
        "preferred_username": "new-owner",
        "email": "owner@example.test",
        "email_verified": False,
    }
    user = await service.identity._user(claims)
    assert user.id.hex == created["user_id"]
    assert user.display_name == "new-owner" and user.contact_email == "owner@example.test"
    assert user.email is None
    principal = service.identity._principal(user, claims, "oidc_bearer")
    assert principal.email is None and not principal.api_roles
    assert await ProjectAccess(store).memberships(principal) == []
    verified_claims = {**claims, "email_verified": True}
    verified = await service.identity._user(verified_claims)
    assert verified.email == "owner@example.test"
    assert service.identity._principal(verified, verified_claims, "oidc_bearer").email == "owner@example.test"
    unverified = await service.identity._user(claims)
    assert unverified.email is None and unverified.contact_email == "owner@example.test"


async def test_directory_contact_rejects_oversized_or_known_credentials(store):
    service, _, _, _, _ = await setup(store)
    for contact in ("x" * 321 + "@example.test", "private-admin-client-secret@example.test", 123):
        with pytest.raises(ValueError):
            await service._local_user(
                str(uuid.uuid4()), {"username": "owner", "email": contact, "emailVerified": False}
            )


async def test_revision14_adds_contact_column_to_existing_schema_without_verifying_email(store):
    from sqlalchemy import text

    from firefly_forge.identity_persistence import ForgeIdentityUserEntity, ForgeIdentityUserRepository
    from firefly_forge.migrations import SCHEMA_REVISION, migrate

    assert SCHEMA_REVISION >= 14
    user_id = uuid.uuid4()
    async with store._unit() as session:
        await ForgeIdentityUserRepository().save(
            ForgeIdentityUserEntity(
                id=user_id, issuer="https://id.example", subject="verified", email="verified@example.test"
            ),
        )
        await session.execute(text("ALTER TABLE forge_identity_users DROP COLUMN contact_email"))
        await session.execute(
            text("UPDATE forge_schema_migrations SET revision=13 WHERE revision=:revision"),
            {"revision": SCHEMA_REVISION},
        )
    async with store._unit() as session:
        await migrate(session, store.schema)
        await migrate(session, store.schema)
        row = (
            await session.execute(
                text("SELECT email, contact_email FROM forge_identity_users WHERE subject='verified'")
            )
        ).one()
        assert row.email == "verified@example.test" and row.contact_email is None
        assert (
            await session.scalar(
                text("SELECT count(*) FROM forge_schema_migrations WHERE revision=:revision"),
                {"revision": SCHEMA_REVISION},
            )
            == 1
        )


async def test_revision14_backfills_only_confirmed_provision_receipts_in_current_realm(store, monkeypatch):
    from sqlalchemy import text

    from firefly_forge.identity_persistence import ForgeIdentityUserEntity
    from firefly_forge.migrations import SCHEMA_REVISION, migrate

    service, actor, _, _, _ = await setup(store)
    monkeypatch.setenv("FORGE_OIDC_ISSUER", service.identity.config.issuer)
    created = await service.provision(
        module().UserProvisionInput(request_id=uuid.uuid4().hex, username="existing-owner", email="owner@example.test"),
        actor,
    )
    rejected = []
    for outcome in ("unknown", "wrong_subject", "wrong_realm"):
        user_id, subject = uuid.uuid4(), str(uuid.uuid4())
        async with store._unit():
            await service.identity.users.save(
                ForgeIdentityUserEntity(
                    id=user_id,
                    issuer="https://other.example/realms/forge"
                    if outcome == "wrong_realm"
                    else service.identity.config.issuer,
                    subject=subject,
                )
            )
        request = module().UserProvisionInput(
            request_id=uuid.uuid4().hex, username=outcome, email=f"{outcome}@example.test"
        )
        operation, _ = await service._claim("provision", request.model_dump(), actor)
        await service._finish(
            operation.id,
            actor,
            "needs_reconciliation" if outcome == "unknown" else "completed",
            {"user_id": user_id.hex, "subject": str(uuid.uuid4()) if outcome == "wrong_subject" else subject},
        )
        rejected.append(user_id)
    async with store._unit() as session:
        await session.execute(text("UPDATE forge_identity_users SET display_name=NULL, email=NULL"))
        await session.execute(text("ALTER TABLE forge_identity_users DROP COLUMN contact_email"))
        await session.execute(
            text("UPDATE forge_schema_migrations SET revision=13 WHERE revision=:revision"),
            {"revision": SCHEMA_REVISION},
        )
    async with store._unit() as session:
        await migrate(session, store.schema)
        user = await service.identity.users.find_by_id(uuid.UUID(created["user_id"]))
        assert user.display_name == "existing-owner" and user.contact_email == "owner@example.test"
        assert user.email is None
        for user_id in rejected:
            assert (await service.identity.users.find_by_id(user_id)).contact_email is None


async def test_unknown_provision_cannot_repeat_and_reconciliation_checks_request_marker(store):
    service, actor, target, calls, remote = await setup(store, outcome="uncertain")
    request = module().UserProvisionInput(request_id=uuid.uuid4().hex, username="new-owner", email="owner@example.test")
    result = await service.provision(request, actor)
    assert result["status"] == "needs_reconciliation"
    assert (await service.provision(request, actor))["status"] == "needs_reconciliation"
    assert sum(method == "POST" and path.endswith("/users") for method, path in calls) == 1
    remote["attributes"]["forge_request_id"] = [uuid.uuid4().hex]
    with pytest.raises(ValueError, match="marker"):
        await service.reconcile(
            result["id"],
            module().AdministrationReconciliation(
                outcome="created", subject=str(target), note="Checked exact provider account"
            ),
            actor,
        )
    remote["attributes"]["forge_request_id"] = [request.request_id]
    reconciled = await service.reconcile(
        result["id"],
        module().AdministrationReconciliation(outcome="created", subject=str(target), note="Confirmed request marker"),
        actor,
    )
    assert reconciled["status"] == "completed"


@pytest.mark.parametrize("status", [204, 500, "uncertain"])
async def test_invite_is_separate_and_only_provider_accepted_not_delivered(store, status):
    service, actor, _, calls, _ = await setup(store, invite_status=status)
    created = await service.provision(
        module().UserProvisionInput(request_id=uuid.uuid4().hex, username="new-owner", email="owner@example.test"),
        actor,
    )
    request = module().UserInviteInput(request_id=uuid.uuid4().hex)
    result = await service.invite(created["user_id"], request, actor)
    expected = "completed" if status == 204 else "needs_reconciliation" if status == "uncertain" else "failed"
    assert result["status"] == expected and result["mail_delivery_verified"] is False
    if status == 204:
        assert result["invitation_status"] == "provider_accepted"
    await service.invite(created["user_id"], request, actor)
    assert sum(method == "PUT" for method, path in calls) == 1


async def test_disabled_or_overprivileged_admin_client_never_posts_user(store):
    service, actor, _, calls, _ = await setup(store, broad=True)
    result = await service.provision(
        module().UserProvisionInput(request_id=uuid.uuid4().hex, username="new-owner"), actor
    )
    assert result["status"] == "failed" and not any(
        method == "POST" and path.endswith("/users") for method, path in calls
    )
    service.config = module().KeycloakAdministrationConfiguration()
    assert service.catalog()["configured"] is False
    with pytest.raises(ValueError, match="configured"):
        await service.provision(module().UserProvisionInput(request_id=uuid.uuid4().hex, username="owner"), actor)


def test_provision_never_accepts_passwords_or_project_role_grants():
    for extra in ({"password": "private"}, {"roles": ["forge-admin"]}, {"email_verified": True}):
        with pytest.raises(ValidationError):
            module().UserProvisionInput(request_id=uuid.uuid4().hex, username="owner", **extra)


async def test_inflight_reconciliation_rejected_and_restart_recovery_never_replays_post(store):
    service, actor, _, calls, _ = await setup(store)
    request = module().UserProvisionInput(request_id=uuid.uuid4().hex, username="owner")
    row, execute = await service._claim("provision", request.model_dump(), actor)
    assert execute
    reconcile = module().AdministrationReconciliation(outcome="not_created", note="Inspected provider state")
    with pytest.raises(ValueError, match="in-flight"):
        await service.reconcile(row.id.hex, reconcile, actor)
    await store.acquire_ownership()
    assert await service.recover() == 1
    result = await service.reconcile(row.id.hex, reconcile, actor)
    assert result["status"] == "closed_not_created"
    assert (await service.provision(request, actor))["status"] == "closed_not_created"
    assert not calls


async def test_actor_suspended_during_authentication_cannot_create_remote_account(store, monkeypatch):
    service, actor, _, calls, _ = await setup(store)
    original = service._token

    async def token(config):
        result = await original(config)
        async with store._unit():
            user = await service.identity.users.find_by_id(actor.id)
            user.enabled = False
            await service.identity.users.save(user)
        return result

    monkeypatch.setattr(service, "_token", token)
    result = await service.provision(module().UserProvisionInput(request_id=uuid.uuid4().hex, username="owner"), actor)
    assert result["status"] == "failed"
    assert not any(method == "POST" and path.endswith("/users") for method, path in calls)


async def test_administration_events_are_immutable_and_response_body_is_bounded(store):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    service, actor, _, _, _ = await setup(store)
    await service.provision(module().UserProvisionInput(request_id=uuid.uuid4().hex, username="owner"), actor)
    with pytest.raises(DBAPIError):
        async with store._unit() as session:
            await session.execute(text("UPDATE forge_identity_admin_events SET data='{}'::jsonb"))

    async def huge(req):
        return httpx.Response(200, content=b"x" * 100001)

    service.client = httpx.AsyncClient(transport=httpx.MockTransport(huge))
    with pytest.raises(ValueError, match="limit"):
        await service._http("GET", "https://id.example/admin/realms/forge/users")


async def test_native_keycloak_view_users_composite_query_roles_are_accepted(store):
    service, actor, _, calls, _ = await setup(store, composite=True)
    result = await service.provision(
        module().UserProvisionInput(request_id=uuid.uuid4().hex, username="composite-reader"), actor
    )
    assert result["status"] == "completed"
    assert sum(method == "POST" and path.endswith("/users") for method, path in calls) == 1
