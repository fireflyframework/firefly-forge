# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Topology admission precedes execution recovery and never upgrades API authority."""

import pytest


async def schema(store):
    from firefly_forge.runtime_role_persistence import ForgeRuntimeTopologyEntity

    async with store._unit() as session:
        connection = await session.connection()
        await connection.run_sync(
            lambda sync: ForgeRuntimeTopologyEntity.metadata.create_all(
                sync, tables=[ForgeRuntimeTopologyEntity.__table__]
            )
        )


async def test_api_replicas_share_topology_but_never_gain_execution_permission(store):
    from firefly_forge.runtime_roles import RuntimeRoles

    await schema(store)
    first, second = RuntimeRoles(store, "api"), RuntimeRoles(store, "api")
    await first.start()
    await second.start()
    assert first.health()["admission_ready"]
    assert first.health()["execution"]["state"] == "api_only"
    assert not first.health()["execution_ready"]
    assert store._ownership_connection is None
    with pytest.raises(RuntimeError, match="API"):
        first.require_execution()


@pytest.mark.parametrize(
    "initial,conflicting", [("api", "combined"), ("worker", "combined"), ("combined", "api"), ("combined", "worker")]
)
async def test_topology_cannot_change_after_first_admission(store, initial, conflicting):
    from firefly_forge.runtime_roles import RuntimeRoles

    await schema(store)
    await RuntimeRoles(store, initial).start()
    with pytest.raises(RuntimeError, match="topology"):
        await RuntimeRoles(store, conflicting).start()


async def test_api_cannot_join_a_legacy_owner_before_split_topology_is_recorded(store):
    from firefly_forge.runtime_roles import RuntimeRoles

    await schema(store)
    await store.acquire_ownership()
    try:
        with pytest.raises(RuntimeError, match="execution owner"):
            await RuntimeRoles(store, "api").start()
    finally:
        await store.release_ownership()


async def test_topology_rejects_different_private_storage_for_the_same_schema(tmp_path, store_factory):
    from firefly_forge.runtime_roles import RuntimeRoles

    first = await store_factory(tmp_path / "first" / "state")
    second = await store_factory(tmp_path / "second" / "state")
    await schema(first)
    await RuntimeRoles(first, "api").start()
    with pytest.raises(RuntimeError, match="private storage"):
        await RuntimeRoles(second, "api").start()


async def test_topology_rejects_another_private_storage_owner(store, monkeypatch):
    import os

    from firefly_forge.runtime_roles import RuntimeRoles

    if not hasattr(os, "getuid"):
        pytest.skip("Numeric private storage ownership is a POSIX contract")
    await RuntimeRoles(store, "api").start()
    current_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: current_uid + 1)
    with pytest.raises(RuntimeError, match="private storage"):
        await RuntimeRoles(store, "api").start()


async def test_legacy_owner_check_ignores_advisory_locks_in_other_databases(store):
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    from firefly_forge.runtime_roles import RuntimeRoles

    other = create_async_engine(make_url(store.database_url).set(database="postgres"))
    try:
        async with other.connect() as connection:
            await connection.execute(
                text("SELECT pg_advisory_lock(hashtextextended(:key,0))"), {"key": "forge:execution:" + store.schema}
            )
            try:
                await RuntimeRoles(store, "api").start()
            finally:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:key,0))"),
                    {"key": "forge:execution:" + store.schema},
                )
    finally:
        await other.dispose()


@pytest.mark.parametrize(
    "consumer", ["execution", "delivery_requests", "deployments", "campaigns", "scm_feedback", "observability"]
)
async def test_native_health_reports_stopped_deployment_consumer(postgres_schema, tmp_path, consumer):
    from firefly_forge.api import create_application
    from firefly_forge.controllers import CatalogController
    from firefly_forge.worker_runtime import WorkerSupervision

    app = create_application(tmp_path, "local-bootstrap-fixture-token")
    async with app.router.lifespan_context(app):
        import asyncio

        context = app.state.runtime.context
        task = context.get_bean(WorkerSupervision).tasks()[consumer]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        health = context.get_bean(CatalogController).health()
        assert health["status"] == "degraded"
        assert not health["execution_ready"]


async def test_headless_worker_drains_and_exits_when_required_consumer_stops(postgres_schema, tmp_path, monkeypatch):
    import asyncio

    from firefly_forge.operations import DeploymentManager
    from firefly_forge.worker_runtime import run_worker

    monkeypatch.setenv("FORGE_AUTH_MODE", "local_bootstrap")
    monkeypatch.setenv("FORGE_API_TOKEN", "local-bootstrap-fixture-token")

    async def stopped_consumer(self):
        return

    monkeypatch.setattr(DeploymentManager, "_poll", stopped_consumer)
    with pytest.raises(RuntimeError, match="scheduler|consumer"):
        await asyncio.wait_for(run_worker(tmp_path, stop=asyncio.Event()), timeout=5)


async def test_worker_topology_does_not_create_a_second_execution_leader(tmp_path, store_factory):
    from firefly_forge.runtime_roles import RuntimeRoles

    first = await store_factory(tmp_path / "state")
    second = await store_factory(tmp_path / "state")
    await schema(first)
    await RuntimeRoles(first, "worker").start()
    await RuntimeRoles(second, "worker").start()
    await first.acquire_ownership()
    try:
        with pytest.raises(RuntimeError, match="already owns"):
            await second.acquire_ownership()
    finally:
        await first.release_ownership()


def test_invalid_role_fails_before_any_runtime_is_created():
    from firefly_forge.runtime_roles import RuntimeRoleConfig

    with pytest.raises(ValueError):
        RuntimeRoleConfig(role="silent-fallback")


async def test_api_engine_and_effect_services_never_recover_or_start_execution(store, tmp_path, monkeypatch):
    from firefly_forge.engine import Engine
    from firefly_forge.operations import DeploymentManager
    from firefly_forge.runtime_roles import RuntimeRoles

    await schema(store)
    store.runtime_roles = RuntimeRoles(store, "api")
    await store.runtime_roles.start()

    async def forbidden(*args, **kwargs):
        pytest.fail("API lifecycle invoked worker recovery or ownership")

    monkeypatch.setattr(store, "acquire_ownership", forbidden)
    monkeypatch.setattr(store, "recover", forbidden)
    monkeypatch.setattr(store, "recover_deployments", forbidden)
    engine = Engine(store, tmp_path / "workspaces")
    manager = DeploymentManager(store, engine)
    await engine.start()
    await manager.start()
    assert engine.poller is None and manager.poller is None
    with pytest.raises(RuntimeError, match="API"):
        await engine.execute_run("f" * 32)
    with pytest.raises(RuntimeError, match="API"):
        await manager.execute_deployment("f" * 32)
    await manager.stop()
    await engine.stop()


async def test_native_api_replicas_start_without_execution_lease_or_worker_effects(postgres_schema, tmp_path):
    from firefly_forge.api import create_application
    from firefly_forge.engine import Engine
    from firefly_forge.execution_requests import ExecutionRequests
    from firefly_forge.operations import DeploymentManager
    from firefly_forge.runtime_roles import RuntimeRoles
    from firefly_forge.store import Store

    first = create_application(tmp_path, "local-bootstrap-fixture-token", role="api")
    second = create_application(tmp_path, "local-bootstrap-fixture-token", role="api")
    async with first.router.lifespan_context(first), second.router.lifespan_context(second):
        for app in (first, second):
            context = app.state.runtime.context
            assert context.get_bean(RuntimeRoles).health()["execution"]["state"] == "api_only"
            assert context.get_bean(Store)._ownership_connection is None
            assert context.get_bean(Engine).poller is None
            assert context.get_bean(DeploymentManager).poller is None
            assert context.get_bean(ExecutionRequests).poller is None


async def test_native_headless_worker_never_constructs_http_app(postgres_schema, tmp_path, monkeypatch):
    import asyncio

    import firefly_forge.api as api
    from firefly_forge.worker_runtime import run_worker

    monkeypatch.setenv("FORGE_AUTH_MODE", "local_bootstrap")
    monkeypatch.setenv("FORGE_API_TOKEN", "local-bootstrap-fixture-token")

    def forbidden(*args, **kwargs):
        pytest.fail("Headless worker constructed an HTTP listener/application")

    monkeypatch.setattr(api, "create_app", forbidden)
    monkeypatch.setattr(asyncio, "start_server", forbidden)
    stop = asyncio.Event()
    runtime = api.create_runtime(tmp_path, role="worker")
    original_startup = runtime.startup

    async def startup():
        from firefly_forge.engine import Engine
        from firefly_forge.store import Store

        await original_startup()
        assert runtime.context.get_bean(Store)._ownership_connection is not None
        assert runtime.context.get_bean(Engine).poller is not None
        stop.set()

    monkeypatch.setattr(runtime, "startup", startup)
    monkeypatch.setattr(api, "create_runtime", lambda *args, **kwargs: runtime)
    await run_worker(tmp_path, stop=stop)


async def test_topology_is_database_immutable(store):
    from sqlalchemy import text

    from firefly_forge.runtime_roles import RuntimeRoles

    await RuntimeRoles(store, "api").start()
    with pytest.raises(Exception, match="immutable"):
        async with store._unit() as session:
            await session.execute(text("UPDATE forge_runtime_topology SET mode='combined'"))


async def test_split_admin_start_never_interrupts_other_api_owner_and_recovery_is_explicit(store):
    from uuid import uuid4

    from test_user_administration import setup

    from firefly_forge.runtime_roles import RuntimeRoles
    from firefly_forge.user_administration import AdministrationRecovery

    service, actor, _, calls, _ = await setup(store)
    store.runtime_roles = RuntimeRoles(store, "worker")
    await store.runtime_roles.start()
    row, _ = await service._claim("provision", {"request_id": uuid4().hex}, actor)
    await service.start()
    async with store._unit():
        assert (await service.operations.find_by_id(row.id)).status == "executing"
    assert calls == []
    with pytest.raises(ValueError):
        AdministrationRecovery(request_id=uuid4().hex, owner_stopped=False, note="Not confirmed")
    request = AdministrationRecovery(
        request_id=uuid4().hex, owner_stopped=True, note="Operator inspected stopped owner"
    )
    receipt = await service.recover_operation(row.id.hex, request, actor)
    assert receipt["status"] == "needs_reconciliation"
    assert receipt == await service.recover_operation(row.id.hex, request, actor)
    assert calls == []


async def test_api_runner_admission_freezes_unknown_grants_without_host_probes(store, tmp_path, monkeypatch):
    from firefly_forge.browser_runner import BrowserRunner
    from firefly_forge.models import RepositoryInput
    from firefly_forge.runner_configuration import RunnerConfiguration
    from firefly_forge.runtime_roles import RuntimeRoles
    from firefly_forge.sandbox import SandboxExecutor
    from firefly_forge.search_tools import SearchTools

    store.runtime_roles = RuntimeRoles(store, "api")
    await store.runtime_roles.start()
    repository = await store.add_repository(RepositoryInput(name="Product", url=str(tmp_path)))
    from firefly_forge.journal import Journal

    executor = SandboxExecutor(store, Journal(store))

    async def forbidden(*args, **kwargs):
        pytest.fail("API admission probed a local code runner")

    monkeypatch.setattr(executor, "probe", forbidden)
    configuration = RunnerConfiguration(
        store, executor, SearchTools(tmp_path / "search"), BrowserRunner(executor, tmp_path / "browser")
    )
    snapshot = await configuration.capture_run(repository["id"])
    tools = await configuration.capabilities(snapshot)
    assert next(tool for tool in tools if tool["id"] == "execute_command")["availability"] == "worker_unverified"
    assert (await configuration.readiness(repository["id"]))["execution"]["status"] == "unknown"


async def test_worker_rejects_unavailable_frozen_grant_before_source_or_model_dispatch(store, tmp_path):
    from unittest.mock import AsyncMock

    from firefly_forge.agent_roles import ProjectRoles
    from firefly_forge.engine import Engine
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.runtime_roles import RuntimeRoles
    from firefly_forge.workspace_tools import tool_catalog

    repository = await store.add_repository(RepositoryInput(name="Product", url=str(tmp_path)))
    tools = tool_catalog({"execute_command": True})
    next(tool for tool in tools if tool["id"] == "execute_command")["availability"] = "worker_unverified"
    run = await store.create_run(
        RunInput(
            repository_id=repository["id"],
            brief="Build",
            agent="agentic",
            roles=ProjectRoles(allowed_tools={"implementer": ["execute_command"]}),
        ),
        prepared_tools=tools,
    )
    store.runtime_roles = RuntimeRoles(store, "worker")
    await store.runtime_roles.start()
    store.runners = AsyncMock()
    store.runners.capabilities.return_value = tool_catalog()
    provider_calls = []

    def provider(name):
        provider_calls.append(name)
        pytest.fail("Unavailable runner reached model dispatch")

    engine = Engine(store, tmp_path / "workspaces", provider_factory=provider)
    await store.acquire_ownership()
    engine.enforce_ownership = True
    try:
        await engine.execute_run(run["id"])
    finally:
        await store.release_ownership()
    result = await store.get_run(run["id"])
    assert result["status"] == "failed"
    assert "attest" in result["error"]
    assert not (engine.workspaces / run["id"]).exists()
    assert provider_calls == []


async def test_api_campaign_cancel_keeps_pending_status_until_worker_acknowledges(store, tmp_path):
    from uuid import uuid4

    from firefly_forge.engine import Engine
    from firefly_forge.execution_requests import ExecutionRequests
    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput
    from firefly_forge.project_campaigns import CampaignAction, CampaignLaunch, ProjectCampaigns
    from firefly_forge.project_models import ProjectDefinition
    from firefly_forge.projects import Projects
    from firefly_forge.runtime_roles import RuntimeRoles

    repository = await store.add_repository(RepositoryInput(name="Product", url=str(tmp_path)))
    project = await Projects(store).configure(
        ProjectDefinition.model_validate(
            {
                "name": "Product",
                "description": "Service",
                "topology": "monorepo",
                "owners": ["Team"],
                "components": [
                    {
                        "id": "api",
                        "name": "API",
                        "repository_id": repository["id"],
                        "responsibility": "Service behavior",
                    }
                ],
            }
        )
    )
    api, worker = RuntimeRoles(store, "api"), RuntimeRoles(store, "worker")
    await api.start()
    await worker.start()
    store.runtime_roles = api
    engine = Engine(store, tmp_path / "workspaces")
    requests = ExecutionRequests(store, api)
    store.execution_requests = requests

    async def revision(repository):
        return "1" * 40

    campaigns = ProjectCampaigns(store, engine, Journal(store), source_resolver=revision)
    campaign = await campaigns.launch(project["id"], CampaignLaunch(request_id=uuid4().hex, brief="Build"))
    cancelled = await campaigns.cancel(project["id"], campaign["id"], CampaignAction(request_id=uuid4().hex))
    assert cancelled["status"] == "cancelling"
    run_id = cancelled["children"][0]["run_id"]
    assert (await store.get_run(run_id))["status"] == "queued"
    store.runtime_roles = worker
    execution = ExecutionRequests(store, worker)
    execution.cancel_effect = engine.cancel_effect
    await store.acquire_ownership()
    try:
        await execution.execute((await requests.for_run(run_id))[0]["id"])
        acknowledged = await campaigns.tick(project["id"], campaign["id"])
    finally:
        await store.release_ownership()
    assert acknowledged["status"] == "cancelled"


async def test_native_api_admitted_test_run_executes_only_after_headless_worker_starts(
    postgres_schema, tmp_path, monkeypatch
):
    import asyncio
    import sys

    from test_execution import seed_repository

    from firefly_forge.api import create_application
    from firefly_forge.journal import Journal
    from firefly_forge.models import Command, RepositoryInput, RunInput
    from firefly_forge.worker_runtime import run_worker

    monkeypatch.setenv("FORGE_AUTH_MODE", "local_bootstrap")
    monkeypatch.setenv("FORGE_API_TOKEN", "local-bootstrap-fixture-token")
    source = await seed_repository(tmp_path / "source")
    app = create_application(tmp_path / "shared", role="api")
    async with app.router.lifespan_context(app):
        store = app.state.store
        repository = await store.add_repository(RepositoryInput(name="Product", url=str(source)))
        run = await store.create_run(
            RunInput(
                repository_id=repository["id"],
                brief="Verify original source",
                operation="test",
                commands=[Command(argv=[sys.executable, "-c", "assert 2 + 3 == 5"])],
            )
        )
        await asyncio.sleep(0.1)
        assert (await store.get_run(run["id"]))["status"] == "queued"
        assert not (app.state.engine.workspaces / run["id"]).exists()
        stop = asyncio.Event()
        worker = asyncio.create_task(run_worker(tmp_path / "shared", stop=stop))
        try:
            async with asyncio.timeout(20):
                while True:
                    current = await store.get_run(run["id"])
                    if current["status"] in {"succeeded", "failed", "interrupted"}:
                        break
                    if worker.done():
                        await worker
                    await asyncio.sleep(0.1)
            assert current["status"] == "succeeded", current.get("error")
            assert current["final_verification"][0]["exit_code"] == 0
            journal = app.state.runtime.context.get_bean(Journal)
            assert Journal.verify_chain(await journal.entries(run["id"]))
            assert (
                next(task for task in (await journal.plan(run["id"]))["tasks"] if task["id"] == "verify")["status"]
                == "done"
            )
            assert app.state.engine.poller is None
        finally:
            stop.set()
            await worker


async def test_native_request_receipts_require_exact_project_read_scope(postgres_schema, tmp_path, monkeypatch):
    from uuid import uuid4

    import httpx
    from native_client_knowledge_fixtures import activate_native_client_target

    from firefly_forge.api import create_application
    from firefly_forge.identity import IdentityService, Principal
    from firefly_forge.models import RunInput
    from firefly_forge.project_access import ProjectAccess

    app = create_application(tmp_path, "local-bootstrap-fixture-token", role="api")
    async with app.router.lifespan_context(app):
        from test_project_access import seed

        store = app.state.store
        await activate_native_client_target(store, tmp_path / "framework-prerequisite")
        repository, projects = await seed(store, tmp_path, count=2)
        viewer = Principal(uuid4(), "https://identity.example/realms/forge", uuid4().hex)
        await ProjectAccess(store).set_membership(projects[0]["id"], viewer, "owner")
        run = await store.create_run(
            RunInput(
                repository_id=repository["id"],
                project_id=projects[1]["id"],
                component_id="service",
                brief="Foreign project",
            )
        )
        await store.execution_requests.request_cancel(run["id"])

        async def authenticated(request):
            return viewer

        monkeypatch.setattr(app.state.runtime.context.get_bean(IdentityService), "authenticate", authenticated)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = await client.get(f"/api/v1/runs/{run['id']}/execution-requests")
        assert response.status_code == 403
        assert run["id"] not in response.text
        assert "request_digest" not in response.text
