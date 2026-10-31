# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native observation custody; only the dedicated PostgreSQL fixture is permitted."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import aliased

from firefly_forge.access_persistence import ForgeMembershipEntity, ForgeMembershipRepository
from firefly_forge.engine import Engine
from firefly_forge.engine_activity import EngineActivity
from firefly_forge.engine_activity_persistence import (
    ForgeEngineActivityEntity,
    ForgeEngineActivityRepository,
    current_activity_predicate,
)
from firefly_forge.factory_dashboard_models import DashboardScope
from firefly_forge.factory_dashboard_persistence import visible_runs_statement
from firefly_forge.identity import Principal
from firefly_forge.identity_persistence import ForgeIdentityUserEntity, ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.persistence import (
    ForgePlanEntity,
    ForgePlanRepository,
    ForgeRepositoryEntity,
    ForgeRunEntity,
    ForgeTaskEntity,
    ForgeTaskRepository,
)
from firefly_forge.project_persistence import ForgeProjectEntity, ForgeProjectRepository


@pytest.fixture
async def stage(store, tmp_path):
    user, repo, run, plan = (uuid4() for _ in range(4))
    actor = {"user_id": user.hex, "authentication_method": "local_bootstrap", "global_admin": True}
    async with store._unit():
        await ForgeIdentityUserRepository().save(
            ForgeIdentityUserEntity(id=user, issuer="local:bootstrap", subject="operator", enabled=True)
        )
        await store.repository_records.save(ForgeRepositoryEntity(id=repo, data={"id": repo.hex}))
        await store.run_records.save(
            ForgeRunEntity(
                id=run,
                repository_id=repo,
                status="developing",
                data={
                    "id": run.hex,
                    "repository_id": repo.hex,
                    "execution_actor": actor,
                    "agent": "agentic",
                    "status": "developing",
                    "brief": "Fixture stage",
                },
            )
        )
        await ForgePlanRepository().save(
            ForgePlanEntity(
                id=plan,
                run_id=run,
                revision=1,
                digest="a" * 64,
                data={
                    "tasks": [
                        {"id": "implement", "kind": "local", "title": "Implement", "instructions": "Fixture only"}
                    ],
                },
            )
        )
        await ForgeTaskRepository().save(
            ForgeTaskEntity(run_id=run, plan_id=plan, task_id="implement", status="queued", attempt=1, data={})
        )
    await store.acquire_ownership()
    activity = EngineActivity(store, ForgeEngineActivityRepository())
    engine = Engine(store, tmp_path / "workspaces", journal=Journal(store), stage_activity=activity)
    engine.enforce_ownership = True
    value = SimpleNamespace(
        store=store, activity=activity, engine=engine, run=run, user=user, repo=repo, plan=plan, path=tmp_path
    )
    yield value
    await activity.close()
    await store.release_ownership()


async def current(c):
    async with c.store._unit() as session:
        return list(
            await session.scalars(
                select(ForgeEngineActivityEntity)
                .select_from(ForgeEngineActivityEntity)
                .join(ForgeRunEntity, ForgeRunEntity.id == ForgeEngineActivityEntity.run_id)
                .join(ForgeTaskEntity, ForgeTaskEntity.run_id == ForgeRunEntity.id)
                .where(current_activity_predicate(c.store.schema))
            )
        )


async def begin(c):
    return await c.engine._begin_task(c.run.hex, "implement", c.path, {})


async def test_native_engine_stage_renewal_end_and_late_epoch_cannot_retire_successor(stage):
    c = stage
    async with c.store._unit():
        await ForgePlanRepository().save(
            ForgePlanEntity(
                id=uuid4(),
                run_id=c.run,
                revision=2,
                digest="b" * 64,
                data={
                    "tasks": [
                        {"id": "implement", "kind": "local", "title": "Implement", "instructions": "Fixture only"}
                    ],
                },
            )
        )
    intent = await begin(c)
    rows = await current(c)
    assert len(rows) == 1 and rows[0].invocation_key == intent["idempotency_key"]
    assert rows[0].plan_id == c.plan and rows[0].current_plan_id != c.plan
    before = rows[0].observed_at
    await c.activity.pulse()
    assert (await current(c))[0].observed_at > before
    old = next(iter(c.activity.scopes.values()))
    successor = EngineActivity(c.store, ForgeEngineActivityRepository())
    try:
        await successor.begin(c.run.hex, "implement", intent["idempotency_key"])
        assert (await current(c))[0].owner_epoch == successor.epoch
        await c.activity.end(c.run.hex)
        assert (await current(c))[0].owner_epoch == successor.epoch
        async with c.store._unit():
            await ForgeEngineActivityRepository().end([old.row_id], c.activity.epoch, c.store.schema)
        assert len(await current(c)) == 1
        await successor.end(c.run.hex)
        assert not await current(c)
    finally:
        await successor.close()


@pytest.mark.parametrize("change", ["actor", "attempt", "scope", "plan", "fence", "expiry"])
async def test_native_current_predicate_refuses_drift_revocation_and_cold_expiry(stage, change):
    c = stage
    await begin(c)
    assert len(await current(c)) == 1
    async with c.store._unit() as session:
        if change == "actor":
            await session.execute(
                update(ForgeIdentityUserEntity).where(ForgeIdentityUserEntity.id == c.user).values(enabled=False)
            )
        elif change == "attempt":
            await session.execute(update(ForgeTaskEntity).where(ForgeTaskEntity.run_id == c.run).values(attempt=2))
        elif change == "scope":
            row = await c.store.run_records.find_by_id(c.run)
            row.data = {**row.data, "policy_snapshot": {"digest": "b" * 64}}
            await c.store.run_records.save(row)
        elif change == "plan":
            await ForgePlanRepository().save(
                ForgePlanEntity(id=uuid4(), run_id=c.run, revision=2, digest="b" * 64, data={"tasks": []})
            )
        elif change == "expiry":
            await session.execute(update(ForgeEngineActivityEntity).values(expires_at=func.clock_timestamp()))
    if change == "fence":
        await c.store.release_ownership()
    assert not await current(c)
    await c.activity.pulse()
    assert not c.activity.scopes
    assert not await current(c)
    # Constructing a cold observer never revives a row or adopts an old local task.
    cold = EngineActivity(c.store, ForgeEngineActivityRepository())
    assert not cold.scopes
    await cold.close()


async def test_native_cancelled_blocked_engine_task_ends_observation_without_advancing_journal(stage):
    c = stage
    entered = asyncio.Event()

    async def work():
        await begin(c)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await c.activity.end(c.run.hex)

    task = asyncio.create_task(work())
    try:
        await entered.wait()
        assert len(await current(c)) == 1
        prior = await c.engine.journal.entries(c.run.hex)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not await current(c)
        assert await c.engine.journal.entries(c.run.hex) == prior
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_native_observation_composes_with_reader_scope_and_current_membership_revocation(stage):
    c = stage
    project, foreign = uuid4(), uuid4()
    async with c.store._unit():
        await ForgeProjectRepository().save(
            ForgeProjectEntity(id=project, data={"definition": {"name": "Observed product", "components": []}})
        )
        user = await ForgeIdentityUserRepository().find_by_id(c.user)
        user.issuer, user.subject = "https://id.example", "owner"
        await ForgeIdentityUserRepository().save(user)
        await ForgeIdentityUserRepository().save(
            ForgeIdentityUserEntity(id=foreign, issuer="https://id.example", subject="foreign", enabled=True)
        )
        await ForgeMembershipRepository().save(
            ForgeMembershipEntity(project_id=project, user_id=c.user, active=True, role="developer")
        )
        row = await c.store.run_records.find_by_id(c.run)
        row.data = {
            **row.data,
            "project_id": project.hex,
            "execution_actor": {"user_id": c.user.hex, "authentication_method": "oidc_bearer"},
        }
        await c.store.run_records.save(row)
    await begin(c)
    assert len(await current(c)) == 1
    stranger = Principal(foreign, "https://id.example", "foreign")
    scoped = visible_runs_statement(
        DashboardScope(kind="run", run_id=c.run.hex), administrator=False, actor=stranger
    ).subquery()
    run = aliased(ForgeRunEntity, scoped)
    async with c.store._unit() as session:
        visible = await session.scalar(
            select(ForgeEngineActivityEntity.id)
            .join(run, run.id == ForgeEngineActivityEntity.run_id)
            .join(ForgeTaskEntity, ForgeTaskEntity.run_id == run.id)
            .where(current_activity_predicate(c.store.schema, run=run))
        )
        assert visible is None
        await session.execute(
            update(ForgeMembershipEntity).where(ForgeMembershipEntity.user_id == c.user).values(active=False)
        )
    assert not await current(c)
    await c.activity.pulse()
    assert not c.activity.scopes


async def test_native_dashboard_observes_only_current_engine_stage_then_receipt_ends_it(stage):
    from firefly_forge.factory_dashboard import FactoryDashboard
    from firefly_forge.factory_dashboard_models import DashboardPageInput
    from firefly_forge.factory_dashboard_persistence import FactoryDashboardRepository
    from firefly_forge.migrations import SCHEMA_REVISION
    from firefly_forge.project_access import ProjectAccess

    c = stage
    assert SCHEMA_REVISION == 33
    await begin(c)
    actor = Principal(c.user, "local:bootstrap", "operator", authentication_method="local_bootstrap")
    dashboard = FactoryDashboard(c.store, ProjectAccess(c.store), FactoryDashboardRepository())
    scope = DashboardScope(kind="run", run_id=c.run.hex)
    value = await dashboard.observe(actor, scope, DashboardPageInput())
    observed = value["stages"][1]
    assert observed["active"] == 1 and observed["unknown"] == 0
    assert observed["observed_until"] > value["observed_at"]
    assert "worker_liveness" in value["unavailable_metrics"]
    key = next(iter(c.activity.scopes.values())).key
    await c.engine._receipt(c.run.hex, key, {"fixture": "known local stage completion"})
    after = await dashboard.observe(actor, scope, DashboardPageInput())
    assert after["stages"][1]["active"] == 0 and after["stages"][1]["done"] == 1
    assert after["stages"][1]["observed_until"] is None


async def test_native_pat_reader_observes_engine_stage_without_execution_or_foreign_scope(stage):
    from datetime import UTC, datetime, timedelta

    from firefly_forge.factory_dashboard import FactoryDashboard
    from firefly_forge.factory_dashboard_models import DashboardPageInput
    from firefly_forge.factory_dashboard_persistence import FactoryDashboardRepository
    from firefly_forge.personal_token_models import PersonalTokenInput
    from firefly_forge.personal_tokens import PersonalTokens
    from firefly_forge.project_access import AccessDenied, ProjectAccess

    c = stage
    project, foreign = uuid4(), uuid4()
    async with c.store._unit():
        for value in (project, foreign):
            await ForgeProjectRepository().save(
                ForgeProjectEntity(id=value, data={"definition": {"name": "Observed project", "components": []}})
            )
        user = await ForgeIdentityUserRepository().find_by_id(c.user)
        user.issuer, user.subject = "https://id.example", "observer"
        user.api_roles_audience = "forge-api"
        user.api_roles_issued_at = datetime.now(UTC).timestamp()
        user.api_roles_expires_at = datetime.now(UTC) + timedelta(hours=1)
        await ForgeMembershipRepository().save(
            ForgeMembershipEntity(project_id=project, user_id=c.user, active=True, role="developer")
        )
        row = await c.store.run_records.find_by_id(c.run)
        row.data = {
            **row.data,
            "project_id": project.hex,
            "agent": "agentic",
            "sandbox_snapshot": {"mode": "docker"},
            "execution_actor": {
                "user_id": c.user.hex,
                "authentication_method": "oidc_bearer",
                "api_audience": "forge-api",
            },
        }
    owner = Principal(c.user, user.issuer, user.subject, authentication_method="oidc_session", api_audience="forge-api")
    tokens = PersonalTokens(c.store)
    issued = await tokens.create(
        owner,
        PersonalTokenInput(
            request_id=uuid4().hex,
            name="Stage observations",
            project_ids=[project.hex],
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    assert issued.secret is not None
    reader = (await tokens.resolve(issued.secret.get_secret_value())).principal
    await begin(c)
    dashboard = FactoryDashboard(c.store, ProjectAccess(c.store, personal_tokens=tokens), FactoryDashboardRepository())
    selected = DashboardScope(kind="project", project_id=project.hex)
    value = await dashboard.observe(reader, selected, DashboardPageInput())
    assert value["stages"][1]["active"] == 1 and value["standalone_available"] is False
    with pytest.raises(AccessDenied):
        await dashboard.observe(reader, DashboardScope(kind="project", project_id=foreign.hex), DashboardPageInput())
    with pytest.raises(AccessDenied):
        await dashboard.access.authorize_actor(ProjectAccess.actor_snapshot(reader), project.hex, "execute")
    async with c.store._unit():
        membership = (await ForgeMembershipRepository().for_user(c.user))[0]
        membership.active = False
    with pytest.raises(AccessDenied):
        await dashboard.observe(reader, selected, DashboardPageInput())


async def test_native_schema32_upgrade_adds_only_observation_table_and_preserves_all_retained_rows(
    store_factory, tmp_path, monkeypatch
):
    import hashlib
    import importlib.util
    from pathlib import Path

    from sqlalchemy import text
    from test_analytics_upgrade_native import retained_rows

    import firefly_forge.store as store_module
    from firefly_forge.migrations import SCHEMA_REVISION, migrate

    source = Path(__file__).parent / "fixtures/engine_activity/schema32_migrations.py"
    assert (
        hashlib.sha256(source.read_bytes()).hexdigest()
        == "bfbb0d2a016c9e2563e4aeffacfb004b2b1252c604d07ee7040b1f206f6bf9ea"
    )
    spec = importlib.util.spec_from_file_location("owned_engine_activity_schema32", source)
    assert spec is not None and spec.loader is not None
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    assert legacy.SCHEMA_REVISION == 32 and SCHEMA_REVISION == 33
    monkeypatch.setattr(store_module, "migrate", legacy.migrate)
    store = await store_factory(tmp_path / "schema32/factory.db")
    user, repo, run = (uuid4() for _ in range(3))
    async with store._unit():
        await ForgeIdentityUserRepository().save(
            ForgeIdentityUserEntity(id=user, issuer="https://id.example", subject="preserved-owner", enabled=True)
        )
        await store.repository_records.save(ForgeRepositoryEntity(id=repo, data={"id": repo.hex, "name": "Preserved"}))
        await store.run_records.save(
            ForgeRunEntity(
                id=run,
                repository_id=repo,
                status="paused",
                data={"id": run.hex, "attempt": 3, "spent": 2, "status": "paused", "retained": ["prior receipt"]},
            )
        )
    before = await retained_rows(store)
    assert "forge_engine_activity" not in before
    monkeypatch.setattr(store_module, "migrate", migrate)
    async with store._unit() as session:
        await migrate(session, store.schema)
        await migrate(session, store.schema)
        assert list(await session.scalars(text("SELECT revision FROM forge_schema_migrations ORDER BY revision"))) == [
            32,
            33,
        ]
        assert await session.scalar(text("SELECT count(*) FROM forge_engine_activity")) == 0
    assert await retained_rows(store, tuple(before)) == before
    assert set(await retained_rows(store)) - set(before) == {"forge_engine_activity"}
