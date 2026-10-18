# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One explicit Factory request may continue only its original still-waiting command."""

import asyncio
import importlib
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError


def module():
    try:
        return importlib.import_module("firefly_forge.runner_continuations")
    except ModuleNotFoundError:
        pytest.fail("Factory-authorized explicit runner continuation is not implemented")


def test_continuation_input_cannot_select_owner_or_supply_outcome():
    model = module().RunnerContinuationInput
    body = {"request_id": "a" * 32, "execution_id": "b" * 32}
    assert model.model_validate(body).model_dump() == body
    for change in ({"request_id": True}, {"execution_id": "../other"}, {"fence": {}}, {"result": {}}):
        with pytest.raises(ValidationError):
            model.model_validate({**body, **change})


async def waiting(store, tmp_path, *, scoped=False, identities=None):
    module()
    from test_runner_pool_worker import fixture

    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess
    from firefly_forge.runtime_roles import RuntimeRoles

    worker, controller, run, journal, workspace = await fixture(store, tmp_path)
    actor = Principal(uuid4(), "local", "owned-operator", authentication_method="local_bootstrap")
    execution_actor = actor
    if scoped:
        from firefly_forge.project_models import ProjectDefinition
        from firefly_forge.projects import Projects

        execution_actor = Principal(uuid4(), "https://issuer.invalid", "owned-execution")
        actor = Principal(uuid4(), "https://issuer.invalid", "owned-requester")
        project = await Projects(store).configure(
            ProjectDefinition(
                name="Owned recovery",
                description="Fictional scoped continuation",
                topology="monorepo",
                owners=["Platform"],
                components=[dict(id="owned", name="Owned", repository_id=run["repository_id"], responsibility="Owned")],
            )
        )
        access = ProjectAccess(store)
        await access.set_membership(project["id"], execution_actor, "developer")
        await access.set_membership(project["id"], actor, "developer")
        await store.update(
            run["id"],
            project_id=project["id"],
            agent="agentic",
            component_snapshot={"project_id": project["id"], "repository_id": run["repository_id"], "path": "."},
        )
        if identities is not None:
            identities.update(project=project, operator=actor, execution_actor=execution_actor)
    await store.update(run["id"], execution_actor=ProjectAccess.actor_snapshot(execution_actor))
    ready, release = asyncio.Event(), asyncio.Event()
    original_observe = controller.observe
    controller.continuations = 0

    async def observe(invocation):
        ready.set()
        if not release.is_set():
            return {"status": "queued", "request": invocation.model_dump(mode="json"), "receipt": None}
        return await original_observe(invocation)

    async def continue_invocation(invocation, fence, request_id, request_digest):
        controller.continuations += 1
        release.set()
        return {
            "execution_id": invocation.execution_id,
            "request_id": request_id,
            "request_digest": request_digest,
            "disposition": "accepted",
            "epoch": 1,
        }

    controller.observe = observe
    controller.continue_invocation = continue_invocation
    context = CURRENT_PRINCIPAL.set(actor)
    task = asyncio.create_task(worker.execute(run["id"], workspace, workspace, ["git", "diff"], 5, "verify", ordinal=1))
    try:
        reached = asyncio.create_task(ready.wait())
        done, _ = await asyncio.wait((reached, task), timeout=10, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        assert reached in done, "Original controller observation was never reached"
        reached.cancel()
        await asyncio.gather(reached, return_exceptions=True)
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        CURRENT_PRINCIPAL.reset(context)
        raise
    runtime = RuntimeRoles(store)
    await runtime.start()
    manager = module().RunnerContinuations(store, runtime, worker)
    return manager, controller, run, journal, task, release, context


async def test_factory_request_is_durable_and_exact_then_leader_continues_once(store, tmp_path):
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    manager, controller, run, journal, task, release, context = await waiting(store, tmp_path)
    try:
        rows = await journal.sandbox_executions(run["id"])
        body = module().RunnerContinuationInput(request_id=uuid4().hex, execution_id=rows[0]["execution_id"])
        admitted = await manager.request(run["id"], body)
        assert admitted["status"] == "queued" and controller.continuations == 0
        assert await manager.request(run["id"], body) == admitted
        await manager.worker.continuation_scope(admitted["request"])
        await manager.execute(body.request_id)
        assert (await manager.get(run["id"], body.request_id))["status"] == "completed"
        assert (await asyncio.wait_for(task, 10)).exit_code == 0
        assert controller.submissions == controller.continuations == 1
        before = await manager.get(run["id"], body.request_id)
        await manager.execute(body.request_id)
        assert await manager.get(run["id"], body.request_id) == before
        assert controller.continuations == 1
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        CURRENT_PRINCIPAL.reset(context)


async def test_controller_exact_claim_never_selects_other_queued_command(store, tmp_path):
    from test_runner_pools import invocation

    from firefly_forge.project_access import CURRENT_PRINCIPAL
    from firefly_forge.runner_pools import RunnerPools

    manager, controller, run, journal, task, release, context = await waiting(store, tmp_path)
    try:
        rows = await journal.sandbox_executions(run["id"])
        request = module().RunnerContinuationInput(request_id=uuid4().hex, execution_id=rows[0]["execution_id"])
        await manager.request(run["id"], request)
        scope = manager.worker.waiting[request.execution_id]
        pools = RunnerPools(store.session_provider, store.schema)
        await pools.initialize()
        profile = controller.profile_value
        other = invocation(profile)
        await pools.submit(profile, other, fence=scope["scope"]["fence"])
        await pools.submit(profile, scope["invocation"], fence=scope["scope"]["fence"])

        async def exact(invocation, fence, request_id, request_digest):
            claim = await pools.claim_continuation(
                profile, "owned-new-controller", fence, invocation.execution_id, request_id, request_digest
            )
            assert claim["disposition"] == "accepted"
            assert claim["claim"]["request"]["execution_id"] == invocation.execution_id
            controller.continuations += 1
            release.set()
            return {key: value for key, value in claim.items() if key != "claim"}

        controller.continue_invocation = exact
        await manager.execute(request.request_id)
        assert (await manager.get(run["id"], request.request_id))["status"] == "completed"
        assert (await asyncio.wait_for(task, 10)).exit_code == 0
        assert (await pools.observe(profile, other.execution_id))["status"] == "queued"
        assert [entry["kind"] for entry in await pools.history(profile, other.execution_id)] == ["submitted"]
        assert controller.continuations == 1
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        CURRENT_PRINCIPAL.reset(context)


@pytest.mark.parametrize("change", [None, "cancel", "operator", "controller_cancel"])
async def test_targeted_controller_completion_never_pumps_foreign_queue(store, tmp_path, change):
    from runner_test_support import installed_docker_binary
    from test_runner_pools import invocation

    from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess
    from firefly_forge.runner_pool_dispatch import DockerPoolDispatcher, RunnerDockerAuthority
    from firefly_forge.runner_pool_execution import RunnerPoolExecutions
    from firefly_forge.runner_pools import RunnerPools

    identities = {}
    manager, controller, run, journal, task, release, context = await waiting(
        store, tmp_path, scoped=bool(change), identities=identities
    )
    executor = None
    try:
        wal = (await journal.sandbox_executions(run["id"]))[0]
        body = module().RunnerContinuationInput(request_id=uuid4().hex, execution_id=wal["execution_id"])
        await manager.request(run["id"], body)
        active = manager.worker.waiting[body.execution_id]
        pools = RunnerPools(store.session_provider, store.schema)
        await pools.initialize()
        profile = controller.profile_value
        other = invocation(profile)
        await pools.submit(profile, other, fence=active["scope"]["fence"])
        await pools.submit(profile, active["invocation"], fence=active["scope"]["fence"])
        config = tmp_path / "owned-docker-config"
        config.mkdir(mode=0o700)
        effects = []

        async def transport(argv, timeout, limit, input_data=None):
            if argv[0] == "info":
                return 0, profile.daemon_id.encode()
            if change == "controller_cancel" and argv[:2] == ["container", "inspect"]:
                return 1, b"No such container"
            effects.append(argv)
            raise TimeoutError("Owned fixture create acknowledgement lost; no real Docker")

        dispatcher = DockerPoolDispatcher(
            RunnerDockerAuthority(
                profile,
                installed_docker_binary(),
                "owned-fixture",
                config,
                manager.worker.artifacts.directory,
                "forge-runner-c08-owned",
                10001,
                10001,
            ),
            transport=transport,
        )
        executor = RunnerPoolExecutions(pools, manager.worker.artifacts, dispatcher, manager.worker.git_snapshot)
        manager.worker.artifacts.retain(profile, active["invocation"], "stdin", b"")
        reached, unblock = asyncio.Event(), asyncio.Event()
        original_git = executor.artifacts.minimum_git
        calls = 0

        async def gated_git(*args):
            nonlocal calls
            calls += 1
            if calls == 2 and change is not None:
                reached.set()
                await unblock.wait()
            return await original_git(*args)

        executor.artifacts.minimum_git = gated_git
        controller.continue_invocation = lambda inv, fence, req, dig: executor.continue_invocation(inv, fence, req, dig)
        await manager.execute(body.request_id)
        assert (await manager.get(run["id"], body.request_id))["status"] == "completed"
        if change is not None:
            await asyncio.wait_for(reached.wait(), 5)
            if change == "cancel":
                await store.update(run["id"], cancel_request={"status": "requested"})
            elif change == "controller_cancel":
                observed = await executor.cancel(body.execution_id, active["scope"]["fence"])
                assert observed["status"] == "finished" and observed["receipt"]["outcome"] == "unknown"
            else:
                await ProjectAccess(store).set_membership(identities["project"]["id"], identities["operator"], None)
            unblock.set()
        await asyncio.gather(*list(executor.tasks.values()))
        assert executor.fence is None
        if change is None:
            assert len(effects) == 1 and effects[0][0] == "create"
        else:
            assert effects == []
            assert not any(
                event["kind"] == "create_intent" for event in await pools.history(profile, body.execution_id)
            )
        assert (await pools.observe(profile, active["invocation"].execution_id))["status"] == (
            "finished" if change == "controller_cancel" else "quarantined"
        )
        assert (await pools.observe(profile, other.execution_id))["status"] == "queued"
        assert [event["kind"] for event in await pools.history(profile, other.execution_id)] == ["submitted"]
        if change != "cancel":
            assert not task.done()
    finally:
        if executor is not None:
            await executor.stop()
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        CURRENT_PRINCIPAL.reset(context)


async def test_native_factory_api_binds_admission_service_without_execution_owner(postgres_schema, tmp_path):
    import httpx

    from firefly_forge.api import create_application
    from firefly_forge.execution_requests import ExecutionRequests
    from firefly_forge.runner_continuation_controllers import RunnerContinuationController
    from firefly_forge.runner_continuations import RunnerContinuations
    from firefly_forge.store import Store

    app = create_application(tmp_path, "owned-explicit-continuation-bootstrap", role="api")
    async with app.router.lifespan_context(app):
        context = app.state.runtime.context
        service = context.get_bean(RunnerContinuations)
        assert context.get_bean(RunnerContinuationController).continuations is service
        assert context.get_bean(ExecutionRequests).runner_continuations is service
        assert service.store is context.get_bean(Store) and service.store._ownership_connection is None
        with pytest.raises(RuntimeError, match="API"):
            await service.execute("a" * 32)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            route = "/api/v1/runs/" + "b" * 32 + "/runner-continuations"
            assert (await client.post(route, json={})).status_code == 401
            response = await client.post(
                route,
                headers={"Authorization": "Bearer owned-explicit-continuation-bootstrap"},
                json={"request_id": True, "execution_id": "c" * 32},
            )
            assert response.status_code == 422


@pytest.mark.parametrize("change", ["cancel", "stage", "actor", "operator", "source", "fence"])
async def test_leader_refuses_changed_current_admission_before_controller_dispatch(store, tmp_path, change):
    from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess

    identities = {}
    manager, controller, run, journal, task, release, context = await waiting(
        store, tmp_path, scoped=True, identities=identities
    )
    try:
        wal = (await journal.sandbox_executions(run["id"]))[0]
        body = module().RunnerContinuationInput(request_id=uuid4().hex, execution_id=wal["execution_id"])
        await manager.request(run["id"], body)
        if change == "cancel":
            await store.update(run["id"], cancel_request={"status": "requested"})
        elif change == "stage":
            await journal.receipt(
                run["id"],
                next(
                    stage["idempotency_key"]
                    for stage in (await journal.plan(run["id"]))["tasks"]
                    if stage["id"] == "verify"
                ),
                {"exit_code": 0},
            )
        elif change in {"actor", "operator"}:
            await ProjectAccess(store).set_membership(
                identities["project"]["id"], identities["execution_actor" if change == "actor" else "operator"], None
            )
        elif change == "source":
            next(iter(manager.worker.waiting.values()))["workspace"].joinpath("main.txt").write_text(
                "changed while queued"
            )
        else:
            await store.release_ownership()
            await store.acquire_ownership()
        await manager.execute(body.request_id)
        data = await manager.requests.get(body.request_id)
        assert data["status"] == "failed" and controller.continuations == 0
        assert not any(event["kind"] == "worker_dispatch" for event in await manager.requests.events(body.request_id))
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        CURRENT_PRINCIPAL.reset(context)


async def test_api_role_never_executes_a_durable_continuation(store, tmp_path):
    from firefly_forge.project_access import CURRENT_PRINCIPAL
    from firefly_forge.runtime_roles import RuntimeRoles

    manager, controller, run, journal, task, release, context = await waiting(store, tmp_path)
    try:
        wal = (await journal.sandbox_executions(run["id"]))[0]
        body = module().RunnerContinuationInput(request_id=uuid4().hex, execution_id=wal["execution_id"])
        await manager.request(run["id"], body)
        manager.runtime = RuntimeRoles(store, "api")
        with pytest.raises(RuntimeError, match="API"):
            await manager.execute(body.request_id)
        assert (await manager.get(run["id"], body.request_id))["status"] == "queued"
        assert controller.continuations == 0
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        CURRENT_PRINCIPAL.reset(context)


async def test_credential_admission_never_holds_factory_run_lock_across_await(store, tmp_path):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from firefly_forge.project_access import CURRENT_PRINCIPAL

    manager, controller, run, journal, task, release, context = await waiting(store, tmp_path)
    engine = create_async_engine(store.database_url)
    observed = []

    async def credential_admission(value):
        if isinstance(value, dict) and "run_binding" in value:
            async with engine.begin() as connection:
                await connection.execute(
                    text(f'SELECT id FROM "{store.schema}".forge_runs WHERE id=:id FOR UPDATE NOWAIT'),
                    {"id": UUID(run["id"])},
                )
            observed.append("run_unlocked")

    store.vault_credential_admission = credential_admission
    try:
        wal = (await journal.sandbox_executions(run["id"]))[0]
        await manager.request(
            run["id"], module().RunnerContinuationInput(request_id=uuid4().hex, execution_id=wal["execution_id"])
        )
        assert observed == ["run_unlocked"]
    finally:
        await engine.dispose()
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        CURRENT_PRINCIPAL.reset(context)


@pytest.mark.parametrize(
    "change", ["cancel", "operator", "actor", "expired", "quarantined", "effect", "stale_fence", "digest", "repository"]
)
async def test_controller_rechecks_native_authority_and_original_pre_effect_state(store, tmp_path, change):
    from datetime import timedelta

    from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess
    from firefly_forge.runner_pools import RunnerPools

    identities = {}
    manager, controller, run, journal, task, release, context = await waiting(
        store, tmp_path, scoped=True, identities=identities
    )
    failure = []
    try:
        wal = (await journal.sandbox_executions(run["id"]))[0]
        body = module().RunnerContinuationInput(request_id=uuid4().hex, execution_id=wal["execution_id"])
        await manager.request(run["id"], body)
        active = manager.worker.waiting[body.execution_id]
        pools = RunnerPools(store.session_provider, store.schema)
        await pools.initialize()
        profile = controller.profile_value
        await pools.submit(profile, active["invocation"], fence=active["scope"]["fence"])
        if change in {"quarantined", "effect"}:
            claim = await pools.claim(profile, "old-controller", active["scope"]["fence"])
            if change == "quarantined":
                await pools.quarantine(
                    profile, claim["id"], "old-controller", claim["epoch"], {"reason": "owned_fixture"}
                )
            else:
                await pools.effect_intent(
                    profile,
                    claim["id"],
                    "old-controller",
                    claim["epoch"],
                    active["scope"]["fence"],
                    "create",
                    payload={"owned": "intent_only"},
                )
        if change == "expired":
            async with pools._unit():
                row = await pools.invocations.execution(body.execution_id)
                row.admission_expires_at = await pools.invocations.database_time() - timedelta(seconds=1)
                await pools.invocations.save(row)
        before = await pools.history(profile, body.execution_id)

        async def controlled(inv, fence, req, dig):
            if change == "cancel":
                await store.update(run["id"], cancel_request={"status": "requested"})
            if change in {"operator", "actor"}:
                await ProjectAccess(store).set_membership(
                    identities["project"]["id"],
                    identities["operator" if change == "operator" else "execution_actor"],
                    None,
                )
            if change == "repository":
                import copy

                from firefly_forge.project_persistence import ForgeProjectRepository

                async with store._unit():
                    row = await ForgeProjectRepository().find_by_id(UUID(identities["project"]["id"]))
                    data = copy.deepcopy(row.data)
                    data["definition"]["components"][0]["repository_id"] = uuid4().hex
                    row.data = data
                    await ForgeProjectRepository().save(row)
            if change == "stale_fence":
                fence = fence.model_copy(update={"pid": fence.pid + 1_000_000})
            if change == "digest":
                dig = "0" * 64
            try:
                return await pools.claim_continuation(profile, "new-controller", fence, inv.execution_id, req, dig)
            except Exception as exc:
                failure.append(type(exc).__name__)
                raise

        controller.continue_invocation = controlled
        await manager.execute(body.request_id)
        assert failure and (await manager.requests.get(body.request_id))["status"] == "needs_reconciliation"
        assert await pools.history(profile, body.execution_id) == before
        assert (await pools.observe(profile, body.execution_id))["owner_id"] != "new-controller"
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        CURRENT_PRINCIPAL.reset(context)


@pytest.mark.parametrize("busy", [False, True])
async def test_expired_pre_effect_lease_exact_decision_is_durable_without_delayed_queue_wake(store, tmp_path, busy):
    from datetime import timedelta

    from test_runner_pools import invocation

    from firefly_forge.project_access import CURRENT_PRINCIPAL
    from firefly_forge.runner_pools import RunnerPools

    manager, controller, run, journal, task, release, context = await waiting(store, tmp_path)
    try:
        wal = (await journal.sandbox_executions(run["id"]))[0]
        body = module().RunnerContinuationInput(request_id=uuid4().hex, execution_id=wal["execution_id"])
        admitted = await manager.request(run["id"], body)
        active = manager.worker.waiting[body.execution_id]
        pools = RunnerPools(store.session_provider, store.schema)
        await pools.initialize()
        profile = controller.profile_value
        fence = active["scope"]["fence"]
        if busy:
            for ordinal in range(profile.slots):
                other = invocation(profile)
                await pools.submit(profile, other, fence=fence)
                assert await pools.claim(profile, "foreign-controller-" + str(ordinal), fence) is not None
        await pools.submit(profile, active["invocation"], fence=fence)
        if not busy:
            await pools.claim(profile, "expired-controller", fence)
            async with pools._unit():
                row = await pools.invocations.execution(body.execution_id)
                row.lease_expires_at = await pools.invocations.database_time() - timedelta(seconds=1)
                await pools.invocations.save(row)
        decisions = []

        async def controlled(inv, fence, req, dig):
            decision = await pools.claim_continuation(profile, "new-controller", fence, inv.execution_id, req, dig)
            decisions.append(decision)
            return {k: v for k, v in decision.items() if k != "claim"}

        controller.continue_invocation = controlled
        await manager.execute(body.request_id)
        assert decisions[0]["disposition"] == ("refused" if busy else "accepted")
        before = await pools.history(profile, body.execution_id)
        repeated = await pools.claim_continuation(
            profile, "another-controller", fence, body.execution_id, body.request_id, admitted["request_digest"]
        )
        assert "claim" not in repeated and repeated["disposition"] == decisions[0]["disposition"]
        assert await pools.history(profile, body.execution_id) == before
        assert (await pools.observe(profile, body.execution_id))["status"] == ("queued" if busy else "leased")
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        CURRENT_PRINCIPAL.reset(context)


async def test_unknown_continuation_acknowledgement_never_replays_the_original_command(store, tmp_path):
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    manager, controller, run, journal, task, release, context = await waiting(store, tmp_path)
    try:
        wal = (await journal.sandbox_executions(run["id"]))[0]
        body = module().RunnerContinuationInput(request_id=uuid4().hex, execution_id=wal["execution_id"])
        await manager.request(run["id"], body)

        async def lost(invocation, fence, request_id, request_digest):
            controller.continuations += 1
            raise TimeoutError("Owned continuation acknowledgement lost")

        controller.continue_invocation = lost
        await manager.execute(body.request_id)
        unknown = await manager.get(run["id"], body.request_id)
        assert unknown["status"] == "needs_reconciliation"
        assert await manager.request(run["id"], body) == unknown
        await manager.execute(body.request_id)
        assert controller.continuations == controller.submissions == 1
        assert await manager.get(run["id"], body.request_id) == unknown
        assert (await journal.sandbox_executions(run["id"]))[0]["effects"]["start"] == {"status": "intent"}
        with pytest.raises(ValueError, match="identity"):
            await manager.request(
                run["id"], module().RunnerContinuationInput(request_id=uuid4().hex, execution_id=body.execution_id)
            )
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        CURRENT_PRINCIPAL.reset(context)


@pytest.mark.parametrize("change", ["operator", "cancel"])
async def test_late_credential_admission_cannot_freeze_revoked_or_cancelled_factory_authority(store, tmp_path, change):
    from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess

    identities = {}
    manager, controller, run, journal, task, release, context = await waiting(
        store, tmp_path, scoped=True, identities=identities
    )

    async def admission(value):
        if isinstance(value, dict) and "run_binding" in value:
            if change == "operator":
                await ProjectAccess(store).set_membership(identities["project"]["id"], identities["operator"], None)
            else:
                await store.update(run["id"], cancel_request={"status": "requested"})

    store.vault_credential_admission = admission
    try:
        wal = (await journal.sandbox_executions(run["id"]))[0]
        from firefly_forge.project_access import AccessDenied

        with pytest.raises(AccessDenied if change == "operator" else ValueError):
            await manager.request(
                run["id"], module().RunnerContinuationInput(request_id=uuid4().hex, execution_id=wal["execution_id"])
            )
        assert await manager.requests.for_run(run["id"]) == []
        async with store._unit():
            assert await manager.requests.records.for_run(UUID(run["id"]), "runner_continue") == []
        assert controller.continuations == 0
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        CURRENT_PRINCIPAL.reset(context)


@pytest.mark.parametrize("read", ["get", "same_id_post"])
async def test_exact_retained_reads_recheck_current_credentials_without_receipt_mutation(store, tmp_path, read):
    import json

    from firefly_forge.project_access import CURRENT_PRINCIPAL

    manager, controller, run, journal, task, release, context = await waiting(store, tmp_path)
    try:
        wal = (await journal.sandbox_executions(run["id"]))[0]
        body = module().RunnerContinuationInput(request_id=uuid4().hex, execution_id=wal["execution_id"])
        admitted = await manager.request(run["id"], body)
        canary = "actual-owned-engine"
        assert canary in json.dumps(admitted)
        before = await manager.requests.events(body.request_id)
        store.credential_registry = lambda: {"FORGE_TEST_API_TOKEN": canary}
        with pytest.raises(ValueError) as error:
            if read == "get":
                await manager.get(run["id"], body.request_id)
            else:
                await manager.request(run["id"], body)
        assert canary not in str(error.value)
        assert await manager.requests.events(body.request_id) == before
        assert controller.continuations == 0
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        CURRENT_PRINCIPAL.reset(context)


@pytest.mark.parametrize("read", ["get", "same_id_post"])
@pytest.mark.parametrize("change", ["result", "status"])
async def test_exact_terminal_read_requires_the_original_immutable_worker_receipt(store, tmp_path, read, change):
    import copy

    from firefly_forge.project_access import CURRENT_PRINCIPAL

    manager, controller, run, journal, task, release, context = await waiting(store, tmp_path)
    try:
        wal = (await journal.sandbox_executions(run["id"]))[0]
        body = module().RunnerContinuationInput(request_id=uuid4().hex, execution_id=wal["execution_id"])
        await manager.request(run["id"], body)
        await manager.execute(body.request_id)
        await asyncio.wait_for(task, 10)
        before = await manager.requests.events(body.request_id)
        async with store._unit():
            row = await manager.requests.records.find_by_id(UUID(body.request_id))
            data = copy.deepcopy(row.data)
            if change == "result":
                data["result"] = {"disposition": "accepted", "execution_id": "f" * 32}
            else:
                row.status = "failed"
                data["status"] = "failed"
            row.data = data
            await manager.requests.records.save(row)
        with pytest.raises(RuntimeError, match="integrity"):
            if read == "get":
                await manager.get(run["id"], body.request_id)
            else:
                await manager.request(run["id"], body)
        assert await manager.requests.events(body.request_id) == before
        assert controller.continuations == 1
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        CURRENT_PRINCIPAL.reset(context)


@pytest.mark.parametrize("read", ["get", "same_id_post"])
async def test_exact_read_rechecks_authority_after_async_credential_fence(store, tmp_path, read):
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess

    identities = {}
    manager, controller, run, journal, task, release, context = await waiting(
        store, tmp_path, scoped=True, identities=identities
    )
    try:
        wal = (await journal.sandbox_executions(run["id"]))[0]
        body = module().RunnerContinuationInput(request_id=uuid4().hex, execution_id=wal["execution_id"])
        await manager.request(run["id"], body)
        before = await manager.requests.events(body.request_id)

        async def credential(value):
            if isinstance(value, dict) and value.get("kind") == "runner_continue":
                await ProjectAccess(store).set_membership(identities["project"]["id"], identities["operator"], None)

        store.vault_credential_admission = credential
        with pytest.raises(AccessDenied):
            if read == "get":
                await manager.get(run["id"], body.request_id)
            else:
                await manager.request(run["id"], body)
        assert await manager.requests.events(body.request_id) == before
        assert controller.continuations == 0
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        CURRENT_PRINCIPAL.reset(context)


async def test_global_pending_bound_serializes_different_run_admissions(store, tmp_path):
    from sqlalchemy import text

    from firefly_forge.execution_request_persistence import (
        ForgeExecutionRequestEntity,
        ForgeExecutionRequestRepository,
    )
    from firefly_forge.identity_persistence import ForgeIdentityUserEntity, ForgeIdentityUserRepository
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    first = await waiting(store, tmp_path / "first")
    actor = CURRENT_PRINCIPAL.get()
    assert actor is not None
    async with store._unit():
        await ForgeIdentityUserRepository().save(
            ForgeIdentityUserEntity(id=actor.id, issuer=actor.issuer, subject=actor.subject, enabled=True)
        )
    second = await waiting(store, tmp_path / "second")
    first_count, release_count, second_count = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests = []
    try:
        async with store._unit():
            for _ in range(499):
                identity = uuid4()
                await first[0].requests.records.save(
                    ForgeExecutionRequestEntity(
                        id=identity,
                        run_id=UUID(first[2]["id"]),
                        kind="cancel",
                        request_key=identity.hex,
                        status="queued",
                        data={"fixture": "owned pending request"},
                    )
                )
        observed = []
        original = first[0].requests.records.pending_count

        async def hold_first_count():
            count = await original()
            observed.append(count)
            first_count.set()
            await release_count.wait()
            return count

        other_count = second[0].requests.records.pending_count

        async def observe_second_count():
            count = await other_count()
            observed.append(count)
            second_count.set()
            return count

        first[0].requests.records.pending_count = hold_first_count
        second[0].requests.records.pending_count = observe_second_count
        bodies = []
        for fixture in (first, second):
            wal = (await fixture[3].sandbox_executions(fixture[2]["id"]))[0]
            bodies.append(module().RunnerContinuationInput(request_id=uuid4().hex, execution_id=wal["execution_id"]))
        requests.append(asyncio.create_task(first[0].request(first[2]["id"], bodies[0])))
        await asyncio.wait_for(first_count.wait(), 5)
        requests.append(asyncio.create_task(second[0].request(second[2]["id"], bodies[1])))
        # Observe either the old unprotected count or an actual waiter on the shared native lock.
        async with asyncio.timeout(5):
            while not second_count.is_set():
                async with store._unit() as session:
                    blocked = await session.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted "
                            "AND classid::bigint=((hashtextextended(:key,0)>>32)&4294967295) "
                            "AND objid::bigint=(hashtextextended(:key,0)&4294967295))"
                        ),
                        {"key": "forge:execution-request-admission:" + store.schema},
                    )
                if blocked:
                    break
                await asyncio.sleep(0)
        release_count.set()
        results = await asyncio.gather(*requests, return_exceptions=True)
        async with store._unit():
            actual = await ForgeExecutionRequestRepository().pending_count()
        assert actual == 500
        assert sorted(observed) == [499, 500]
        assert sum(isinstance(result, dict) for result in results) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
    finally:
        release_count.set()
        for request in requests:
            request.cancel()
        await asyncio.gather(*requests, return_exceptions=True)
        for fixture in (second, first):
            fixture[5].set()
            fixture[4].cancel()
            await asyncio.gather(fixture[4], return_exceptions=True)
            CURRENT_PRINCIPAL.reset(fixture[6])
