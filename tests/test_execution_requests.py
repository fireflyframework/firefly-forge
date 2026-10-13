# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""API requests remain intents until the execution owner records an actual outcome."""

import pytest
from test_operations import release_fixture

from firefly_forge.store import identifier


async def queue_fixture(store, tmp_path):
    from test_runtime_roles import schema

    from firefly_forge.execution_request_persistence import (
        ForgeExecutionRequestEntity,
        ForgeExecutionRequestEventEntity,
    )
    from firefly_forge.execution_requests import ExecutionRequests
    from firefly_forge.runtime_roles import RuntimeRoles

    manager, run, project, owner, builder, marker = await release_fixture(store, tmp_path)
    await schema(store)
    async with store._unit() as session:
        connection = await session.connection()
        await connection.run_sync(
            lambda sync: ForgeExecutionRequestEntity.metadata.create_all(
                sync, tables=[ForgeExecutionRequestEntity.__table__, ForgeExecutionRequestEventEntity.__table__]
            )
        )
    api, worker = RuntimeRoles(store, "api"), RuntimeRoles(store, "worker")
    await api.start()
    await worker.start()
    return (ExecutionRequests(store, api), ExecutionRequests(store, worker), run, project, owner, builder)


async def test_api_publication_persists_distinct_releaser_without_invoking_effect(store, tmp_path):
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    api, worker, run, _, owner, builder = await queue_fixture(store, tmp_path)
    calls = []

    async def publish(run_id, actor):
        calls.append((run_id, actor))
        return "https://github.com/org/product/pull/1"

    worker.publish_effect = publish
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        request = await api.request_publish(run["id"])
        assert request["status"] == "queued" and calls == []
        assert request["request"]["actor"]["user_id"] == owner.id.hex
        assert request["request"]["actor"]["user_id"] != builder.id.hex
        assert await api.request_publish(run["id"]) == request
        with pytest.raises(RuntimeError, match="API"):
            await api.execute(request["id"])
    finally:
        CURRENT_PRINCIPAL.reset(context)
    await store.acquire_ownership()
    try:
        await worker.execute(request["id"])
    finally:
        await store.release_ownership()
    completed = await worker.get(request["id"])
    assert completed["status"] == "completed"
    assert calls[0][1]["user_id"] == owner.id.hex
    assert completed["result"]["pr_url"].endswith("/1")
    assert worker.verify_chain(await worker.events(request["id"]))


async def test_worker_rechecks_releaser_before_any_publication_effect(store, tmp_path):
    from firefly_forge.project_access import CURRENT_PRINCIPAL, ProjectAccess

    api, worker, run, project, owner, builder = await queue_fixture(store, tmp_path)
    calls = []

    async def publish(*args):
        calls.append(args)
        return "https://github.com/org/product/pull/1"

    worker.publish_effect = publish
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        request = await api.request_publish(run["id"])
    finally:
        CURRENT_PRINCIPAL.reset(context)
    await ProjectAccess(store).set_membership(project["id"], builder, "owner")
    await ProjectAccess(store).set_membership(project["id"], owner, "viewer")
    await store.acquire_ownership()
    try:
        await worker.execute(request["id"])
    finally:
        await store.release_ownership()
    assert calls == []
    assert (await worker.get(request["id"]))["status"] == "failed"


async def test_unknown_publication_is_never_automatically_replayed(store, tmp_path):
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    api, worker, run, _, owner, _ = await queue_fixture(store, tmp_path)
    calls = []

    async def publish(run_id, actor):
        calls.append(run_id)
        await store.update(run_id, publication_status="publishing")
        raise TimeoutError("An actual remote response may be missing")

    worker.publish_effect = publish
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        request = await api.request_publish(run["id"])
    finally:
        CURRENT_PRINCIPAL.reset(context)
    await store.acquire_ownership()
    try:
        await worker.execute(request["id"])
        await worker.execute(request["id"])
    finally:
        await store.release_ownership()
    assert calls == [run["id"]]
    assert (await worker.get(request["id"]))["status"] == "needs_reconciliation"


async def test_cancel_request_does_not_claim_an_inflight_worker_stopped(store, tmp_path):
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    api, worker, run, _, owner, _ = await queue_fixture(store, tmp_path)
    # This fixture's completed tree becomes a controlled in-flight fixture only within this test schema.
    async with store._unit():
        row = await store.run_records.find_by_id(identifier(run["id"]))
        row.status = "developing"
        row.data = {**row.data, "status": "developing"}
        await store.run_records.save(row)
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        request = await api.request_cancel(run["id"])
    finally:
        CURRENT_PRINCIPAL.reset(context)
    pending = await store.get_run(run["id"])
    assert pending["status"] == "developing"
    assert pending["cancel_request"]["status"] == "requested"
    acknowledgements = []

    async def cancel(run_id):
        acknowledgements.append(run_id)
        await store.finish(run_id, "cancelled")
        return {"local_task_drained": True, "remote_provider_outcome": "unknown"}

    worker.cancel_effect = cancel
    await store.acquire_ownership()
    try:
        await worker.execute(request["id"])
    finally:
        await store.release_ownership()
    assert acknowledgements == [run["id"]]
    assert (await store.get_run(run["id"]))["cancel_request"]["status"] == "acknowledged"


async def test_worker_restart_marks_only_worker_claimed_requests_unknown_without_reexecution(store, tmp_path):
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    api, worker, run, _, owner, _ = await queue_fixture(store, tmp_path)
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        request = await api.request_publish(run["id"])
    finally:
        CURRENT_PRINCIPAL.reset(context)
    await store.acquire_ownership()
    try:
        assert await worker._claim(request["id"])
        assert await worker.recover() == 1
        assert await worker.recover() == 0
    finally:
        await store.release_ownership()
    assert (await worker.get(request["id"]))["status"] == "needs_reconciliation"


async def test_api_cancel_is_acknowledged_only_after_real_worker_task_drains(store, tmp_path):
    import asyncio

    from firefly_forge.engine import Engine
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    api, worker, run, _, owner, _ = await queue_fixture(store, tmp_path)
    async with store._unit():
        row = await store.run_records.find_by_id(identifier(run["id"]))
        row.status = "developing"
        row.data = {**row.data, "status": row.status}
        await store.run_records.save(row)
    started, drained = asyncio.Event(), asyncio.Event()

    async def live_task():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    worker_engine = Engine(store, tmp_path / "worker")
    worker_engine.active[run["id"]] = asyncio.create_task(live_task())
    await started.wait()
    api_engine = Engine(store, tmp_path / "api")
    store.runtime_roles, store.execution_requests = api.runtime, api
    context = CURRENT_PRINCIPAL.set(owner)
    try:
        await api_engine.cancel(run["id"])
    finally:
        CURRENT_PRINCIPAL.reset(context)
    assert not drained.is_set()
    assert (await store.get_run(run["id"]))["status"] == "developing"
    store.runtime_roles = worker.runtime
    worker.cancel_effect = worker_engine.cancel_effect
    await store.acquire_ownership()
    try:
        request = (await api.for_run(run["id"]))[0]
        await worker.execute(request["id"])
    finally:
        await store.release_ownership()
    assert drained.is_set()
    current = await store.get_run(run["id"])
    assert current["status"] == "cancelled"
    assert current["cancel_request"]["status"] == "acknowledged"
    assert (await worker.get(request["id"]))["result"]["worker_drained"]


async def test_request_admission_and_receipts_are_database_immutable(store, tmp_path):
    from sqlalchemy import text

    api, _, run, _, _, _ = await queue_fixture(store, tmp_path)
    request = await api.request_cancel(run["id"])
    with pytest.raises(Exception, match="immutable"):
        async with store._unit() as session:
            await session.execute(
                text("UPDATE forge_execution_requests SET data=jsonb_set(data,'{request,actor}', '{}')")
            )
    with pytest.raises(Exception, match="immutable"):
        async with store._unit() as session:
            await session.execute(text("UPDATE forge_execution_request_events SET data='{}'::jsonb"))
    assert (await api.get(request["id"]))["request_digest"] == request["request_digest"]
    assert api.verify_chain(await api.events(request["id"]))


async def test_known_pre_effect_failure_allows_only_three_explicit_admissions(store, tmp_path):
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    api, worker, run, _, owner, _ = await queue_fixture(store, tmp_path)

    worker.publish_effect = None
    context = CURRENT_PRINCIPAL.set(owner)
    await store.acquire_ownership()
    try:
        admissions = []
        for _ in range(3):
            request = await api.request_publish(run["id"])
            admissions.append(request["id"])
            assert await api.request_publish(run["id"]) == request
            await worker.execute(request["id"])
            assert (await worker.get(request["id"]))["status"] == "failed"
        assert len(set(admissions)) == 3
        with pytest.raises(ValueError, match="three"):
            await api.request_publish(run["id"])
    finally:
        CURRENT_PRINCIPAL.reset(context)
        await store.release_ownership()
