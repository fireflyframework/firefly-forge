# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native read projection acceptance; uses only the dedicated conftest PostgreSQL fixture."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from firefly_forge.access_persistence import ForgeMembershipEntity, ForgeMembershipRepository
from firefly_forge.factory_dashboard import FactoryDashboard
from firefly_forge.factory_dashboard_models import DashboardPageInput, DashboardScope
from firefly_forge.factory_dashboard_persistence import FactoryDashboardRepository
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserEntity, ForgeIdentityUserRepository
from firefly_forge.persistence import (
    ForgePlanEntity,
    ForgePlanRepository,
    ForgeRepositoryEntity,
    ForgeRunEntity,
    ForgeTaskEntity,
    ForgeTaskRepository,
)
from firefly_forge.personal_token_models import PersonalTokenInput
from firefly_forge.personal_tokens import PersonalTokens
from firefly_forge.project_access import AccessDenied, ProjectAccess
from firefly_forge.project_persistence import ForgeProjectEntity, ForgeProjectRepository


class NativeObservationProbe(FactoryDashboardRepository):
    async def snapshot(self, *args, **kwargs):
        session = self._require_session()
        assert await session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"
        return await super().snapshot(*args, **kwargs)


@pytest.fixture
async def factory_records(store):
    owner = Principal(uuid4(), "https://id.example/realms/forge", "owner", authentication_method="oidc_session")
    admin = Principal(
        uuid4(), "local:bootstrap", "operator", authentication_method="local_bootstrap", kind="local_bootstrap"
    )
    projects = [uuid4(), uuid4()]
    repositories = [uuid4(), uuid4()]
    runs = [uuid4() for _ in range(4)]
    async with store._unit():
        for actor in (owner, admin):
            await ForgeIdentityUserRepository().save(
                ForgeIdentityUserEntity(
                    id=actor.id, issuer=actor.issuer, subject=actor.subject, enabled=True, api_roles=[]
                )
            )
        for index, identifier in enumerate(repositories):
            await store.repository_records.save(
                ForgeRepositoryEntity(id=identifier, data={"id": identifier.hex, "name": f"Repository {index}"})
            )
        for index, identifier in enumerate(projects):
            components = [{"id": "api", "name": "API", "repository_id": repositories[0].hex, "path": "services/api"}]
            if index == 0:
                components.append({"id": "web", "name": "Web", "repository_id": repositories[1].hex, "path": "."})
            await ForgeProjectRepository().save(
                ForgeProjectEntity(
                    id=identifier,
                    data={
                        "id": identifier.hex,
                        "revision": 1,
                        "definition": {"name": f"Product {index}", "components": components},
                    },
                )
            )
        await ForgeMembershipRepository().save(
            ForgeMembershipEntity(project_id=projects[0], user_id=owner.id, role="owner", active=True)
        )
        for index, run in enumerate(runs):
            status = ["developing", "paused", "developing", "queued"][index]
            project = projects[0] if index < 2 else projects[1] if index == 2 else None
            repo = repositories[1] if index == 1 else repositories[0]
            await store.run_records.save(
                ForgeRunEntity(
                    id=run,
                    repository_id=repo,
                    status=status,
                    data={
                        "id": run.hex,
                        "repository_id": repo.hex,
                        "project_id": project.hex if project else None,
                        "status": status,
                        "brief": f"Run {index}",
                        "component_snapshot": {
                            "id": "web" if index == 1 else "api",
                            "name": "Web" if index == 1 else "API",
                        },
                        "created_at": "2026-10-30T00:00:00Z",
                        "active_task": "implement",
                    },
                )
            )
            first = ForgePlanEntity(
                id=uuid4(),
                run_id=run,
                revision=1,
                digest="a" * 64,
                data={"tasks": [{"id": "implement", "instructions": "Implement the recorded request"}]},
            )
            await ForgePlanRepository().save(first)
            await ForgeTaskRepository().save(
                ForgeTaskEntity(run_id=run, plan_id=first.id, task_id="implement", status="running", attempt=1, data={})
            )
            # The real journal appends revisions while retaining each task's original plan FK.
            await ForgePlanRepository().save(
                ForgePlanEntity(id=uuid4(), run_id=run, revision=2, digest="b" * 64, data=first.data)
            )
    tokens = PersonalTokens(store)
    store.personal_tokens = tokens
    return SimpleNamespace(
        store=store,
        owner=owner,
        admin=admin,
        projects=projects,
        repositories=repositories,
        runs=runs,
        tokens=tokens,
        dashboard=FactoryDashboard(store, ProjectAccess(store, personal_tokens=tokens), NativeObservationProbe()),
    )


async def test_native_scope_spans_product_repositories_without_shared_repository_leaks(factory_records):
    c = factory_records
    async with c.store._unit():
        await c.store.run_records.save_all(
            [
                ForgeRunEntity(
                    id=uuid4(), repository_id=c.repositories[0], status="queued", data={"project_id": c.projects[1].hex}
                )
                for _ in range(501)
            ]
        )
        await ForgeProjectRepository().save_all(
            [
                ForgeProjectEntity(id=uuid4(), data={"definition": {"name": f"Other product {n}", "components": []}})
                for n in range(201)
            ]
        )
    value = await c.dashboard.observe(c.owner, DashboardScope(), DashboardPageInput(limit=1))
    assert value["counts"]["total"] == 2 and value["counts"]["active"] == 1 and value["counts"]["needs_action"] == 1
    assert value["associations"] == {"projects": 1, "standalone_runs": 0, "repositories_with_runs": 2}
    assert value["stages"][1]["active"] == 0 and value["stages"][1]["unknown"] == 2
    assert len(value["runs"]["items"]) == 1 and value["runs"]["next_cursor"]
    second = await c.dashboard.observe(
        c.owner, DashboardScope(), DashboardPageInput(limit=1, cursor=value["runs"]["next_cursor"])
    )
    assert {value["runs"]["items"][0]["id"], second["runs"]["items"][0]["id"]} == {run.hex for run in c.runs[:2]}
    projects = await c.dashboard.projects(c.owner, DashboardPageInput())
    assert projects["items"] == [
        {"id": c.projects[0].hex, "name": "Product 0", "component_count": 2, "repository_count": 2}
    ]
    with pytest.raises(AccessDenied):
        await c.dashboard.observe(
            c.owner, DashboardScope(kind="project", project_id=c.projects[1].hex), DashboardPageInput()
        )
    with pytest.raises(KeyError):
        await c.dashboard.observe(c.owner, DashboardScope(kind="run", run_id=c.runs[2].hex), DashboardPageInput())
    standalone = await c.dashboard.observe(c.admin, DashboardScope(kind="standalone"), DashboardPageInput())
    assert standalone["counts"]["total"] == 1 and standalone["runs"]["items"][0]["association"] == "standalone"
    single = await c.dashboard.observe(c.owner, DashboardScope(kind="run", run_id=c.runs[0].hex), DashboardPageInput())
    assert single["counts"]["total"] == 1 and single["stages"][1]["active"] == 0
    all_runs = await c.dashboard.observe(c.admin, DashboardScope(), DashboardPageInput(limit=1))
    assert all_runs["counts"]["total"] == 505 and len(all_runs["runs"]["items"]) == 1
    assert all_runs["associations"]["projects"] == 203
    first_projects = await c.dashboard.projects(c.admin, DashboardPageInput(limit=200))
    last_projects = await c.dashboard.projects(
        c.admin, DashboardPageInput(limit=200, cursor=first_projects["next_cursor"])
    )
    assert len(first_projects["items"]) == 200 and len(last_projects["items"]) == 3
    assert not ({r["id"] for r in first_projects["items"]} & {r["id"] for r in last_projects["items"]})
    async with c.store._unit():
        membership = (await ForgeMembershipRepository().for_user(c.owner.id))[0]
        membership.active = False
    assert (await c.dashboard.observe(c.owner, DashboardScope(), DashboardPageInput()))["counts"]["total"] == 0
    with pytest.raises(AccessDenied):
        await c.dashboard.observe(
            c.owner, DashboardScope(kind="project", project_id=c.projects[0].hex), DashboardPageInput()
        )


async def test_native_project_read_token_observation_and_current_revocation(factory_records):
    c = factory_records
    issued = await c.tokens.create(
        c.owner,
        PersonalTokenInput(
            request_id=uuid4().hex,
            name="Dashboard observer",
            project_ids=[c.projects[0].hex],
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    assert issued.secret is not None
    principal = (await c.tokens.resolve(issued.secret.get_secret_value())).principal
    value = await c.dashboard.observe(
        principal, DashboardScope(kind="project", project_id=c.projects[0].hex), DashboardPageInput()
    )
    assert value["counts"]["total"] == 2 and value["standalone_available"] is False
    single = await c.dashboard.observe(
        principal, DashboardScope(kind="run", run_id=c.runs[0].hex), DashboardPageInput()
    )
    assert single["counts"]["total"] == 1
    with pytest.raises(KeyError):
        await c.dashboard.observe(principal, DashboardScope(kind="run", run_id=c.runs[2].hex), DashboardPageInput())
    for scope in (DashboardScope(), DashboardScope(kind="standalone")):
        with pytest.raises(AccessDenied):
            await c.dashboard.observe(principal, scope, DashboardPageInput())
    await c.tokens.revoke(c.owner, issued.credential["id"])
    with pytest.raises(AccessDenied):
        await c.dashboard.observe(
            principal, DashboardScope(kind="project", project_id=c.projects[0].hex), DashboardPageInput()
        )


async def test_native_factory_ownership_does_not_invent_per_run_liveness(factory_records):
    c = factory_records
    await c.store.acquire_ownership()
    scope = DashboardScope(kind="run", run_id=c.runs[0].hex)
    value = await c.dashboard.observe(c.owner, scope, DashboardPageInput())
    assert value["counts"]["active"] == 1
    assert value["stages"][1]["recorded_running"] == 1
    assert value["stages"][1]["active"] == 0
    assert value["stages"][1]["unknown"] == 1
    assert value["stages"][1]["observed_until"] is None
    assert "worker_liveness" in value["unavailable_metrics"]
    async with c.store._unit() as session:
        assert await session.scalar(text("SELECT to_regclass('forge_runner_invocations')")) is None
    await c.store.release_ownership()
