# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Signed current grants, rather than admitted administrator flags, authorize queued work."""

import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from test_identity import fixture, request
from test_project_access import seed

from firefly_forge.project_access import AccessDenied, ProjectAccess


async def authenticated(service, token, *, issued_at, roles):
    principal = await service.authenticate(request(bearer="Bearer " + token("forge-api", iat=issued_at, roles=roles)))
    assert principal is not None
    return principal


async def test_newer_verified_role_removal_revokes_queued_administrator_authority(store):
    service, _, _, _, token = await fixture(store)
    issued_at = int(time.time()) - 30
    administrator = await authenticated(service, token, issued_at=issued_at, roles=["forge-admin"])
    actor = ProjectAccess.actor_snapshot(administrator)
    access = ProjectAccess(store)
    await access.authorize_actor(actor, None, "execute")
    current = await authenticated(service, token, issued_at=issued_at + 1, roles=["ordinary-user"])
    assert not current.api_roles
    with pytest.raises(AccessDenied):
        await access.authorize_actor(actor, None, "execute")


@pytest.mark.parametrize("old_token_offset", [0, -1])
async def test_older_or_same_second_tokens_cannot_restore_observed_removed_roles(store, old_token_offset):
    service, _, _, _, token = await fixture(store)
    issued_at = int(time.time()) - 30
    administrator = await authenticated(service, token, issued_at=issued_at, roles=["forge-admin"])
    await authenticated(service, token, issued_at=issued_at, roles=["ordinary-user"])
    stale = await authenticated(service, token, issued_at=issued_at + old_token_offset, roles=["forge-admin"])
    assert "forge-admin" not in stale.api_roles
    with pytest.raises(AccessDenied):
        await ProjectAccess(store).authorize_actor(ProjectAccess.actor_snapshot(administrator), None, "release")
    restored = await authenticated(service, token, issued_at=issued_at + 1, roles=["forge-admin"])
    assert "forge-admin" in restored.api_roles
    await ProjectAccess(store).authorize_actor(ProjectAccess.actor_snapshot(restored), None, "release")


async def test_expired_cached_role_cannot_authorize_background_work(store, monkeypatch):
    import firefly_forge.project_access as module

    service, _, _, _, token = await fixture(store)
    issued_at = int(time.time()) - 30
    expiry = int(time.time()) + 300
    administrator = await service.authenticate(
        request(bearer="Bearer " + token("forge-api", iat=issued_at, exp=expiry))
    )
    assert administrator is not None

    class FutureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(expiry + 1, UTC)

    monkeypatch.setattr(module, "datetime", FutureClock, raising=False)
    with pytest.raises(AccessDenied):
        await ProjectAccess(store).authorize_actor(ProjectAccess.actor_snapshot(administrator), None, "execute")


async def test_removed_global_role_can_use_current_project_membership_but_not_host_code(store, tmp_path):
    service, _, _, _, token = await fixture(store)
    issued_at = int(time.time()) - 30
    administrator = await authenticated(service, token, issued_at=issued_at, roles=["forge-admin"])
    actor = ProjectAccess.actor_snapshot(administrator)
    _, projects = await seed(store, tmp_path)
    project_id = projects[0]["id"]
    access = ProjectAccess(store)
    await access.set_membership(project_id, administrator, "owner")
    await authenticated(service, token, issued_at=issued_at + 1, roles=["ordinary-user"])
    run = {
        "execution_actor": actor,
        "project_id": project_id,
        "agent": "agentic",
        "assurance": {},
        "sandbox_snapshot": {"mode": "docker"},
    }
    await access.authorize_execution(run)
    for update in (
        {"agent": "codex"},
        {"assurance": {"review_agent": "claude"}},
        {"sandbox_snapshot": {"mode": "host"}},
    ):
        with pytest.raises((AccessDenied, ValueError)):
            await access.authorize_execution({**run, **update})


async def test_unknown_legacy_administrator_snapshot_has_no_unverified_bypass(store, tmp_path):
    from firefly_forge.identity import Principal

    _, projects = await seed(store, tmp_path)
    access = ProjectAccess(store)
    user = Principal(uuid4(), "https://id.example/realms/forge", "legacy")
    await access.set_membership(projects[0]["id"], user, "viewer")
    actor = {"user_id": user.id.hex, "authentication_method": "oidc_bearer", "global_admin": True}
    with pytest.raises(AccessDenied):
        await access.authorize_actor(actor, projects[0]["id"], "release")


async def test_login_authority_comes_from_api_access_token_not_id_token_roles(store, monkeypatch):
    service, challenge, params, _, token = await fixture(store)
    original = service._json

    async def response(method, uri, **kwargs):
        if uri.endswith("/token"):
            return {
                "id_token": token("forge-ui", nonce=params["nonce"][0], roles=["forge-admin"]),
                "access_token": token("forge-api", roles=["ordinary-user"]),
                "refresh_token": "fixture-refresh-only",
                "token_type": "Bearer",
            }
        return await original(method, uri, **kwargs)

    monkeypatch.setattr(service, "_json", response)
    session = await service.complete_login("fixture-code", params["state"][0], challenge.browser_nonce)
    assert not session.principal.api_roles
    await authenticated(service, token, issued_at=int(time.time()) - 1, roles=["forge-admin"])
    with pytest.raises(AccessDenied):
        await ProjectAccess(store).authorize_actor(
            {"user_id": session.principal.id.hex, "authentication_method": "oidc_session", "global_admin": True},
            None,
            "execute",
        )


async def test_revoked_administrator_never_dispatches_an_admitted_host_coding_job(store, tmp_path):
    from test_execution import seed_repository

    from firefly_forge.engine import Engine
    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.process import checked
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    service, _, _, _, token = await fixture(store)
    issued_at = int(time.time()) - 30
    administrator = await authenticated(service, token, issued_at=issued_at, roles=["forge-admin"])
    source = await seed_repository(tmp_path / "source")
    repository = await store.add_repository(RepositoryInput(name="Queued host product", url=str(source)))
    context = CURRENT_PRINCIPAL.set(administrator)
    try:
        run = await store.create_run(RunInput(repository_id=repository["id"], brief="Implement a product"))
    finally:
        CURRENT_PRINCIPAL.reset(context)
    await authenticated(service, token, issued_at=issued_at + 1, roles=["ordinary-user"])

    def forbidden_provider(_):
        raise AssertionError("Revoked administrator must not dispatch coding")

    engine = Engine(store, tmp_path / "jobs", forbidden_provider, journal=Journal(store))
    await engine.execute_run(run["id"])
    result = await store.get_run(run["id"])
    assert result["status"] == "failed" and result.get("source_tree") is None
    assert not (tmp_path / "jobs" / run["id"] / ".git").exists()
    assert await checked(["git", "status", "--porcelain"], source) == ""
    assert not any(entry["kind"] == "intent" for entry in await engine.journal.entries(run["id"]))


async def test_schema18_upgrade_does_not_infer_privileges_from_legacy_administrator_history(store):
    from sqlalchemy import text

    from firefly_forge.identity import Principal
    from firefly_forge.migrations import migrate

    legacy = Principal(uuid4(), "https://id.example/realms/forge", "legacy-upgrade")
    from firefly_forge.identity_persistence import ForgeIdentityUserEntity, ForgeIdentityUserRepository

    async with store._unit() as session:
        await ForgeIdentityUserRepository().save(
            ForgeIdentityUserEntity(id=legacy.id, issuer=legacy.issuer, subject=legacy.subject, enabled=True)
        )
        await session.execute(text("DELETE FROM forge_schema_migrations WHERE revision>=18"))
        await session.execute(text("INSERT INTO forge_schema_migrations(revision) VALUES(17) ON CONFLICT DO NOTHING"))
        for column in ("api_roles", "api_roles_audience", "api_roles_issued_at", "api_roles_expires_at"):
            await session.execute(text("ALTER TABLE forge_identity_users DROP COLUMN " + column))
    async with store._unit() as session:
        await migrate(session, store.schema)
        row = (
            await session.execute(
                text("SELECT enabled, api_roles, api_roles_issued_at, api_roles_expires_at FROM forge_identity_users")
            )
        ).one()
        assert row.enabled is True and row.api_roles == []
        assert row.api_roles_issued_at is None and row.api_roles_expires_at is None
    with pytest.raises(AccessDenied):
        await ProjectAccess(store).authorize_actor(
            {"user_id": legacy.id.hex, "authentication_method": "oidc_bearer", "global_admin": True}, None
        )


async def test_verified_role_in_another_api_audience_cannot_authorize_original_actor(store):
    from firefly_forge.identity import IdentityService

    service, _, _, _, token = await fixture(store)
    issued_at = int(time.time()) - 30
    administrator = await authenticated(service, token, issued_at=issued_at, roles=["forge-admin"])
    actor = ProjectAccess.actor_snapshot(administrator)
    other = IdentityService(
        store, config=service.config.model_copy(update={"api_audience": "other-api"}), client=service.client
    )
    current = await other.authenticate(
        request(
            bearer="Bearer "
            + token("other-api", iat=issued_at + 1, resource_access={"other-api": {"roles": ["forge-admin"]}})
        )
    )
    assert current is not None and "forge-admin" in current.api_roles
    with pytest.raises(AccessDenied):
        await ProjectAccess(store).authorize_actor(actor, None)
    await ProjectAccess(store).authorize_actor(ProjectAccess.actor_snapshot(current), None)


async def test_captured_principal_cannot_keep_global_project_scope_after_new_role_observation(store, tmp_path):
    from starlette.requests import Request

    from firefly_forge.access_controllers import AccessController
    from firefly_forge.access_routes import AccessRoutes

    service, _, _, _, token = await fixture(store)
    issued_at = int(time.time()) - 30
    administrator = await authenticated(service, token, issued_at=issued_at, roles=["forge-admin"])
    repository, projects = await seed(store, tmp_path)
    await authenticated(service, token, issued_at=issued_at + 1, roles=["ordinary-user"])
    access = ProjectAccess(store)
    with pytest.raises(AccessDenied):
        await access.require_project(administrator, projects[0]["id"], "release")
    with pytest.raises(AccessDenied):
        await access.require_repository(administrator, repository["id"], "edit")
    with pytest.raises(AccessDenied):
        await AccessRoutes(access, store).authorize(
            administrator,
            Request({"type": "http", "method": "GET", "path": "/api/v1/identity/users", "headers": []}),
        )
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    context = CURRENT_PRINCIPAL.set(administrator)
    try:
        assert await access.filter_projects(projects) == []
        assert await access.filter_repositories([repository]) == []
        assert await access.filter_runs([{"project_id": projects[0]["id"]}]) == []
        assert (await AccessController(access, store).session())["global_admin"] is False
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def test_identity_role_removal_during_provider_token_await_prevents_remote_user_creation(store, monkeypatch):
    from test_user_administration import setup

    from firefly_forge.user_administration import UserProvisionInput

    service, _, _, calls, _ = await setup(store)
    authenticator, _, _, _, token = await fixture(store)
    issued_at = int(time.time()) - 30
    administrator = await authenticated(authenticator, token, issued_at=issued_at, roles=["forge-admin"])
    original = service._token

    async def revoke_after_token(config):
        result = await original(config)
        await authenticated(authenticator, token, issued_at=issued_at + 1, roles=["ordinary-user"])
        return result

    monkeypatch.setattr(service, "_token", revoke_after_token)
    result = await service.provision(
        UserProvisionInput(request_id=uuid4().hex, username="must-not-be-created"), administrator
    )
    assert result["status"] == "failed"
    assert ("POST", "/admin/realms/forge/users") not in calls


async def test_stale_administrator_with_project_membership_cannot_admit_host_runner(store, tmp_path):
    from firefly_forge.models import RunInput
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    service, _, _, _, token = await fixture(store)
    issued_at = int(time.time()) - 30
    administrator = await authenticated(service, token, issued_at=issued_at, roles=["forge-admin"])
    repository, projects = await seed(store, tmp_path)
    await ProjectAccess(store).set_membership(projects[0]["id"], administrator, "owner")
    await authenticated(service, token, issued_at=issued_at + 1, roles=["ordinary-user"])
    context = CURRENT_PRINCIPAL.set(administrator)
    try:
        with pytest.raises(ValueError, match="isolated runner"):
            await store.create_run(
                RunInput(
                    repository_id=repository["id"],
                    project_id=projects[0]["id"],
                    component_id="service",
                    brief="Check existing service",
                    operation="test",
                )
            )
    finally:
        CURRENT_PRINCIPAL.reset(context)
    assert await store.runs() == []
