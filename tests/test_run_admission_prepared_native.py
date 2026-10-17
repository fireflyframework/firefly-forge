# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PREPARED ONLY: owned PostgreSQL controls for the sole native operator."""

import pytest
from pyfly.data.relational.sqlalchemy import LockMode
from pyfly.data.transaction import current_unit_of_work
from test_identity import fixture as identity_fixture
from test_identity import request as identity_request

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.engine import Engine
from firefly_forge.identity_persistence import ForgeIdentityUserRepository
from firefly_forge.journal import Journal
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.project_access import CURRENT_PRINCIPAL
from firefly_forge.project_campaigns import CampaignLaunch, ProjectCampaigns
from firefly_forge.project_models import ProjectDefinition
from firefly_forge.projects import Projects
from firefly_forge.runtime_roles import RuntimeRoles
from firefly_forge.store import identifier, prepared_credential_rejection


async def test_native_prepared_run_event_failure_rolls_back_both(store, tmp_path, monkeypatch):
    repo = await store.add_repository(RepositoryInput(name="Fictional stock", url=str(tmp_path)))
    save = store.event_records.save

    async def fail(row):
        await save(row)
        raise ValueError("Owned queued-event failure")

    monkeypatch.setattr(store.event_records, "save", fail)
    with pytest.raises(ValueError, match="queued-event failure"):
        await store.create_run(RunInput(repository_id=repo["id"], brief="Fictional stock admission"))
    assert await store.runs() == []


async def test_native_prepared_repository_drift_refuses_without_run(store, tmp_path):
    repo = await store.add_repository(RepositoryInput(name="Fictional stock", url=str(tmp_path)))
    prepared = await store.prepare_run(RunInput(repository_id=repo["id"], brief="Fictional stock admission"))
    async with store._unit():
        row = await store.repository_records.find_by_id(identifier(repo["id"]), lock=LockMode.PESSIMISTIC_WRITE)
        row.data = {**row.data, "default_branch": "changed"}
        await store.repository_records.save(row)
    with prepared_credential_rejection(store), pytest.raises(ValueError, match="repository changed"):
        async with store._unit():
            await store.fence_prepared_runs([prepared])
            await store.insert_prepared_run_locked(prepared)
    assert await store.runs() == []


async def test_native_prepared_disabled_registered_actor_cannot_insert(store, tmp_path):
    identity, _, _, _, issue = await identity_fixture(store)
    token = None
    try:
        actor = await identity.authenticate(identity_request(bearer="Bearer " + issue("forge-api")))
        assert actor is not None
        token = CURRENT_PRINCIPAL.set(actor)
        repo = await store.add_repository(RepositoryInput(name="Fictional stock", url=str(tmp_path)))
        prepared = await store.prepare_run(RunInput(repository_id=repo["id"], brief="Fictional stock admission"))
        async with store._unit():
            users = ForgeIdentityUserRepository()
            row = await users.find_by_id(actor.id, lock=LockMode.PESSIMISTIC_WRITE)
            assert row is not None and row.enabled
            row.enabled = False
            await users.save(row)
        with prepared_credential_rejection(store), pytest.raises(ValueError, match="enabled native user"):
            async with store._unit():
                await store.fence_prepared_runs([prepared])
                await store.insert_prepared_run_locked(prepared)
        assert await store.runs() == []
    finally:
        await identity.client.aclose()
        if token is not None:
            CURRENT_PRINCIPAL.reset(token)


async def test_native_campaign_prepares_files_before_atomic_children(store, tmp_path, monkeypatch):
    repo = await store.add_repository(RepositoryInput(name="Fictional stock", url=str(tmp_path)))
    definition = ProjectDefinition(
        name="Fictional stock",
        description="API admission",
        topology="monorepo",
        owners=["Owner"],
        components=[dict(id="api", name="API", repository_id=repo["id"], responsibility="Serve stock")],
    )
    project = await Projects(store).configure(definition)
    store.runtime_roles = RuntimeRoles(store, "api")
    await store.runtime_roles.start()
    engine = Engine(store, tmp_path / "workspaces")

    async def source(repository):
        # Fixed source-resolver fixture; this case does not qualify a real Git capture.
        return "1" * 40

    manager = ProjectCampaigns(store, engine, Journal(store), source_resolver=source)
    read, write = ConfigurationStore._read, ConfigurationStore._write

    def outside_read(private, path):
        assert current_unit_of_work() is None, "Configuration read occurred under a native unit"
        return read(private, path)

    def outside_write(private, path, data):
        assert current_unit_of_work() is None, "Configuration write occurred under a native unit"
        return write(private, path, data)

    monkeypatch.setattr(ConfigurationStore, "_read", outside_read)
    monkeypatch.setattr(ConfigurationStore, "_write", outside_write)
    request = CampaignLaunch(request_id="e" * 32, brief="Fictional stock admission")
    result = await manager.launch(project["id"], request)
    assert len(result["children"]) == 1
    child = result["children"][0]
    run = await store.get_run(child["run_id"])
    assert run["component_snapshot"]["project_revision"] == project["revision"]
    assert run["status"] == "queued" and run["campaign_context"]["source_base"] == "1" * 40
    replay = await manager.launch(project["id"], request)
    assert replay["id"] == result["id"]
    assert len(await store.runs()) == 1
    assert run["product_definition"] is None
