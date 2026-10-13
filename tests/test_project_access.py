# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Project permissions must not expand through shared repositories or stale roles."""

from uuid import uuid4

import pytest

from firefly_forge.identity import Principal
from firefly_forge.models import RepositoryInput
from firefly_forge.project_access import AccessDenied, ProjectAccess
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects


def principal():
    return Principal(uuid4(), "https://identity.example/realms/forge", uuid4().hex)


async def seed(store, tmp_path, count=1):
    source = tmp_path / "source"
    source.mkdir()
    repo = await store.add_repository(RepositoryInput(name="Service", url=str(source)))
    projects = Projects(store)
    rows = []
    for index in range(count):
        rows.append(
            await projects.configure(
                ProjectDefinition(
                    name=f"Product {index}",
                    description="Reservation product",
                    topology="monorepo",
                    owners=["Platform"],
                    components=[
                        dict(id="service", name="Service", repository_id=repo["id"], responsibility="Reserve inventory")
                    ],
                )
            )
        )
    return repo, rows


@pytest.mark.asyncio
async def test_unassigned_identity_has_no_project_or_repository_access(store, tmp_path):
    repo, projects = await seed(store, tmp_path)
    acl, user = ProjectAccess(store), principal()
    assert await acl.memberships(user) == []
    with pytest.raises(AccessDenied):
        await acl.require_project(user, projects[0]["id"], "read")
    with pytest.raises(AccessDenied):
        await acl.require_repository(user, repo["id"], "read")


@pytest.mark.asyncio
async def test_membership_is_authoritative_and_revocation_takes_effect(store, tmp_path):
    _, projects = await seed(store, tmp_path)
    acl, user = ProjectAccess(store), principal()
    await acl.set_membership(projects[0]["id"], user, "developer")
    await acl.require_project(user, projects[0]["id"], "execute")
    with pytest.raises(AccessDenied):
        await acl.require_project(user, projects[0]["id"], "release")
    await acl.set_membership(projects[0]["id"], user, None)
    with pytest.raises(AccessDenied):
        await acl.require_project(user, projects[0]["id"], "execute")


@pytest.mark.asyncio
async def test_shared_repository_cannot_grant_cross_project_write(store, tmp_path):
    repo, projects = await seed(store, tmp_path, 2)
    acl, user = ProjectAccess(store), principal()
    await acl.set_membership(projects[0]["id"], user, "owner")
    await acl.require_repository(user, repo["id"], "read")
    with pytest.raises(AccessDenied):
        await acl.require_repository(user, repo["id"], "edit")
    await acl.set_membership(projects[1]["id"], user, "maintainer")
    await acl.require_repository(user, repo["id"], "edit")


@pytest.mark.parametrize(
    "role,permission,allowed",
    [
        ("owner", "members", True),
        ("maintainer", "members", False),
        ("maintainer", "release", True),
        ("developer", "execute", True),
        ("developer", "review", False),
        ("reviewer", "review", True),
        ("reviewer", "execute", False),
        ("viewer", "read", True),
        ("viewer", "edit", False),
    ],
)
def test_role_matrix(role, permission, allowed):
    assert ProjectAccess.permits(role, permission) is allowed


def test_only_verified_api_admin_or_explicit_bootstrap_is_global_admin():
    user = principal()
    assert not ProjectAccess.is_admin(user)
    assert not ProjectAccess.is_admin(
        Principal(user.id, user.issuer, user.subject, api_roles=frozenset({"forge-worker"}))
    )
    assert ProjectAccess.is_admin(Principal(user.id, user.issuer, user.subject, api_roles=frozenset({"forge-admin"})))
    assert ProjectAccess.is_admin(
        Principal(user.id, user.issuer, user.subject, authentication_method="local_bootstrap")
    )


@pytest.mark.asyncio
async def test_native_api_enforces_project_scopes_and_filters_lists(tmp_path, postgres_schema, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from firefly_forge.api import create_application
    from firefly_forge.identity import IdentityService
    from firefly_forge.persistence import ForgeRunEntity
    from firefly_forge.store import identifier

    user = principal()

    async def authenticate(_self, _request):
        return user

    monkeypatch.setattr(IdentityService, "authenticate", authenticate)
    app = create_application(tmp_path / "server", "local-test-operator-token-32chars")
    async with app.router.lifespan_context(app):
        store = app.state.store
        repo, projects = await seed(store, tmp_path, 2)
        acl = app.state.runtime.context.get_bean(ProjectAccess)
        await acl.set_membership(projects[0]["id"], user, "developer")
        run_id = uuid4().hex
        async with store._unit():
            await store.run_records.save(
                ForgeRunEntity(
                    id=identifier(run_id),
                    repository_id=identifier(repo["id"]),
                    status="succeeded",
                    data={
                        "id": run_id,
                        "project_id": projects[1]["id"],
                        "repository_id": repo["id"],
                        "status": "succeeded",
                    },
                )
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            assert [row["id"] for row in (await client.get("/api/v1/projects")).json()] == [projects[0]["id"]]
            assert (await client.get("/api/v1/runs")).json() == []
            assert (await client.get("/api/v1/runs/" + run_id)).status_code == 403
            assert (await client.get("/api/v1/projects/" + projects[1]["id"])).status_code == 403
            assert (await client.put("/api/v1/repositories/" + repo["id"] + "/brand", json={})).status_code == 403
            assert (await client.put("/api/v1/providers/bridge/configuration", json={})).status_code == 403
            assert (await client.post("/api/v1/projects", json=projects[0]["definition"])).status_code == 403
            assert (await client.get("/api/v1/identity/users")).status_code == 403
            assert (await client.get("/api/v1/projects/" + projects[0]["id"] + "/members")).status_code == 403
            result = (await client.get("/api/v1/auth/session")).json()
            assert result["global_admin"] is False
            assert result["memberships"][0]["permissions"] == ["edit", "execute", "read"]


@pytest.mark.asyncio
async def test_queued_execution_permission_is_rechecked_after_revocation(store, tmp_path):
    _, projects = await seed(store, tmp_path)
    acl, user = ProjectAccess(store), principal()
    await acl.set_membership(projects[0]["id"], user, "developer")
    run = {
        "project_id": projects[0]["id"],
        "execution_actor": {
            "user_id": user.id.hex,
            "authentication_method": "oidc_session",
            "global_admin": False,
        },
    }
    await acl.authorize_execution(run)
    await acl.set_membership(projects[0]["id"], user, "viewer")
    with pytest.raises(AccessDenied):
        await acl.authorize_execution(run)


@pytest.mark.asyncio
async def test_last_owner_cannot_be_removed_and_access_history_is_immutable(store, tmp_path):
    from sqlalchemy import select, update
    from sqlalchemy.exc import DBAPIError

    from firefly_forge.access_persistence import ForgeAccessEventEntity

    _, projects = await seed(store, tmp_path)
    acl, user = ProjectAccess(store), principal()
    await acl.set_membership(projects[0]["id"], user, "owner")
    with pytest.raises(ValueError, match="last owner"):
        await acl.set_membership(projects[0]["id"], user, None)
    async with store._unit() as session:
        assert len(list(await session.scalars(select(ForgeAccessEventEntity)))) == 1
    with pytest.raises(DBAPIError, match="immutable"):
        async with store._unit() as session:
            await session.execute(update(ForgeAccessEventEntity).values(data={}))


@pytest.mark.asyncio
async def test_project_user_cannot_execute_on_factory_host(store, tmp_path):
    from firefly_forge.models import RunInput
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    repo, projects = await seed(store, tmp_path)
    acl, user = ProjectAccess(store), principal()
    await acl.set_membership(projects[0]["id"], user, "developer")
    context = CURRENT_PRINCIPAL.set(user)
    try:
        with pytest.raises(ValueError, match="isolated runner"):
            await store.create_run(
                RunInput(
                    repository_id=repo["id"],
                    project_id=projects[0]["id"],
                    component_id="service",
                    brief="Implement reservations",
                    agent="agentic",
                )
            )
        assert await store.runs() == []
    finally:
        CURRENT_PRINCIPAL.reset(context)


async def admitted_api_run(store, tmp_path, **options):
    from firefly_forge.models import RunInput
    from firefly_forge.project_access import CURRENT_PRINCIPAL
    from firefly_forge.sandbox_models import SandboxInput

    repo, projects = await seed(store, tmp_path)
    user = principal()
    await ProjectAccess(store).set_membership(projects[0]["id"], user, "developer")
    context = CURRENT_PRINCIPAL.set(user)
    try:
        return await store.create_run(
            RunInput(
                repository_id=repo["id"],
                project_id=projects[0]["id"],
                component_id="service",
                brief="Reservation change",
                agent="agentic",
                sandbox=SandboxInput(
                    mode="docker", context="colima-forge-tests", image="firefly-forge@sha256:" + "a" * 64
                ),
                **options,
            )
        )
    finally:
        CURRENT_PRINCIPAL.reset(context)


@pytest.mark.asyncio
async def test_nonadmin_cannot_escape_isolation_by_handoff(store, tmp_path):
    from firefly_forge.journal import Journal

    run = await admitted_api_run(store, tmp_path)
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await store.update(run["id"], status="paused", pause={"kind": "quota"})
    with pytest.raises(ValueError, match="host coding CLI"):
        await journal.resume(run["id"], "codex")
    assert (await store.get_run(run["id"]))["agent"] == "agentic"
    assert (await store.get_run(run["id"]))["status"] == "paused"


@pytest.mark.asyncio
async def test_nonadmin_cannot_escape_isolation_by_reviewer(store, tmp_path):
    with pytest.raises(ValueError, match="host coding CLI"):
        await admitted_api_run(store, tmp_path, assurance={"review_agent": "codex"})
    assert await store.runs() == []


@pytest.mark.asyncio
async def test_authenticated_client_cannot_invent_a_product_approval(store, tmp_path):
    import copy

    from firefly_forge.collaboration import Collaboration, ProductDefinition
    from firefly_forge.models import RunInput
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    source = tmp_path / "source"
    source.mkdir()
    repo = await store.add_repository(RepositoryInput(name="Inventory", url=str(source)))
    recorded = await Collaboration(store).save_product(
        repo["id"],
        ProductDefinition(
            customer="Warehouse operator",
            problem="Reserve available inventory",
            journey=["Reserve stock"],
            user_stories=["Reserve stock safely"],
            acceptance=["Reject overselling"],
            owners=["Inventory team"],
        ),
    )
    forged = copy.deepcopy(recorded)
    from test_product_lifecycle import feature

    forged["definition"]["features"] = [feature("reserve", "Reserve stock")]
    forged["definition"]["functional_reviewed"] = True
    from firefly_forge.product_provenance import content_digest

    forged["digest"] = content_digest({key: value for key, value in forged.items() if key != "digest"})
    user = principal()
    token = CURRENT_PRINCIPAL.set(
        Principal(user.id, user.issuer, user.subject, authentication_method="local_bootstrap")
    )
    try:
        with pytest.raises(ValueError, match="immutable product revision"):
            await store.create_run(RunInput(repository_id=repo["id"], brief="Reserve stock", product_definition=forged))
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert await store.runs() == []


@pytest.mark.asyncio
async def test_member_lookup_treats_wildcards_literally_and_excludes_suspended_users(store):
    from firefly_forge.access_controllers import AccessController
    from firefly_forge.identity_persistence import ForgeIdentityUserEntity, ForgeIdentityUserRepository

    async with store._unit():
        for name, enabled in [("Anna_% literal", True), ("Anna Smith", True), ("Anna_% suspended", False)]:
            await ForgeIdentityUserRepository().save(
                ForgeIdentityUserEntity(
                    id=uuid4(),
                    issuer="https://identity.example/realms/forge",
                    subject=uuid4().hex,
                    display_name=name,
                    enabled=enabled,
                )
            )
    controller = AccessController(ProjectAccess(store), store)
    rows = await controller.eligible(uuid4().hex, "Anna_%")
    assert [row["display_name"] for row in rows] == ["Anna_% literal"]
    with pytest.raises(ValueError, match="3 and 120"):
        await controller.eligible(uuid4().hex, "An")
