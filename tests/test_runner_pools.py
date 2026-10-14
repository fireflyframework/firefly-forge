# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Local pool contracts; PostgreSQL tests never substitute mocks for leases."""

import importlib
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text

IMAGE = "example/worker@sha256:" + "a" * 64
TRANSFER = "example/transfer@sha256:" + "b" * 64


def models():
    try:
        return importlib.import_module("firefly_forge.runner_pool_models")
    except ModuleNotFoundError:
        pytest.fail("Immutable runner pool contract is not implemented")


def profile(**changes):
    return models().RunnerPoolProfile(
        profile_id="local-test",
        daemon_id="owned-daemon",
        approved_images=(IMAGE,),
        transfer_image=TRANSFER,
        slots=2,
        project_slots=1,
        cpu_budget=2,
        memory_budget_mb=1024,
        **changes,
    )


def invocation(pool, *, project=None, repository=None, **changes):
    return models().RunnerInvocation(
        execution_id=uuid4().hex,
        profile_id=pool.profile_id,
        profile_revision=pool.revision,
        project_id=project,
        repository_id=repository or uuid4(),
        run_id=uuid4(),
        image=IMAGE,
        argv=("python", "-c", "print(1)"),
        source_digest="c" * 64,
        archive_digest="d" * 64,
        source_bytes=1024,
        **changes,
    )


def test_immutable_profile_has_stable_public_digest_and_rejects_ambient_authority():
    pool = profile()
    assert pool.revision == profile().revision
    with pytest.raises(ValidationError):
        pool.slots = 10
    for change in ({"context": "default"}, {"network": "egress"}, {"approved_images": ["example:latest"]}):
        with pytest.raises(ValidationError):
            models().RunnerPoolProfile.model_validate({**pool.model_dump(), **change})
    with pytest.raises(ValidationError):
        models().RunnerPoolProfile.model_validate({**pool.model_dump(), "project_slots": 3})


def test_invocation_only_selects_bounded_execution_not_controller_or_host_paths():
    pool = profile()
    request = invocation(pool)
    pool.validate_invocation(request)
    assert request.project_key == "repository:" + request.repository_id.hex
    for change in ({"cwd": "../peer"}, {"cwd": "/private"}, {"argv": ["sh", "\0"]}, {"endpoint": "https://evil"}):
        with pytest.raises(ValidationError):
            models().RunnerInvocation.model_validate({**request.model_dump(), **change})
    for change in ({"image": TRANSFER}, {"cpus": 3}, {"memory_mb": 2048}, {"profile_revision": "e" * 64}):
        elevated = models().RunnerInvocation.model_validate({**request.model_dump(), **change})
        with pytest.raises(ValueError):
            pool.validate_invocation(elevated)


@pytest.fixture
async def ledger(store, postgres_schema):
    try:
        module = importlib.import_module("firefly_forge.runner_pools")
    except ModuleNotFoundError:
        pytest.fail("Native PostgreSQL runner pool ledger is not implemented")
    # This fixture owns all tables. The distinct-schema test below verifies controller separation.
    ledger = module.RunnerPools(store.session_provider, postgres_schema)
    await ledger.initialize()
    await store.acquire_ownership()
    async with store._unit() as session:
        proof = await session.execute(
            text("SELECT pid, backend_start FROM pg_stat_activity WHERE pid=:pid"), {"pid": store._ownership_pid}
        )
        pid, started = proof.one()
    fence = models().FactoryFence(factory_schema=postgres_schema, pid=pid, backend_started_at=started)
    return ledger, fence


async def test_shared_capacity_project_quota_repository_writer_and_fair_rotation(ledger):
    pools, fence = ledger
    pool = profile()
    a, b = uuid4(), uuid4()
    requests = [invocation(pool, project=a), invocation(pool, project=a), invocation(pool, project=b)]
    for request in requests:
        await pools.submit(pool, request)
    first = await pools.claim(pool, "owner-a", fence)
    second = await pools.claim(pool, "owner-b", fence)
    assert first["request"]["execution_id"] == requests[0].execution_id
    assert second["request"]["execution_id"] == requests[2].execution_id
    assert await pools.claim(pool, "owner-c", fence) is None
    await pools.finish(pool, first["id"], "owner-a", first["epoch"], fence, {"drained": True})
    third = await pools.claim(pool, "owner-c", fence)
    assert third["request"]["execution_id"] == requests[1].execution_id
    # A second repository writer may not reserve concurrently even for another product.
    await pools.submit(pool, invocation(pool, project=b, repository=requests[1].repository_id))
    await pools.finish(pool, second["id"], "owner-b", second["epoch"], fence, {"drained": True})
    assert await pools.claim(pool, "owner-d", fence) is None


async def test_replay_is_immutable_and_wait_queue_has_backpressure(ledger):
    pools, _ = ledger
    pool = profile(waiting_limit=1)
    request = invocation(pool)
    one = await pools.submit(pool, request)
    assert await pools.submit(pool, request) == one
    changed = models().RunnerInvocation.model_validate({**request.model_dump(), "argv": ["python", "-V"]})
    with pytest.raises(ValueError, match="immutable"):
        await pools.submit(pool, changed)
    with pytest.raises(RuntimeError, match="queue"):
        await pools.submit(pool, invocation(pool))


async def test_expired_possible_start_remains_quarantined_and_stale_epoch_cannot_release(ledger, store):
    pools, fence = ledger
    pool = profile()
    request = invocation(pool)
    await pools.submit(pool, request)
    claim = await pools.claim(pool, "owner-a", fence)
    await pools.effect_intent(pool, claim["id"], "owner-a", claim["epoch"], fence, "create")
    async with store._unit() as session:
        await session.execute(
            text("UPDATE forge_runner_invocations SET lease_expires_at=clock_timestamp()-interval '1 second'")
        )
    assert not await pools.renew(pool, claim["id"], "owner-a", claim["epoch"])
    with pytest.raises(RuntimeError, match="lease"):
        await pools.finish(pool, claim["id"], "owner-a", claim["epoch"], fence, {"drained": True})
    assert await pools.claim(pool, "owner-b", fence) is None
    row = await pools.observe(pool, request.execution_id)
    assert row["status"] == "quarantined"
    assert row["epoch"] == claim["epoch"]


async def test_exact_backend_identity_and_lock_loss_prevent_effects(ledger, store):
    pools, fence = ledger
    pool = profile()
    request = invocation(pool)
    await pools.submit(pool, request)
    claim = await pools.claim(pool, "owner-a", fence)
    wrong = models().FactoryFence.model_validate(
        {**fence.model_dump(), "backend_started_at": fence.backend_started_at - timedelta(seconds=1)}
    )
    with pytest.raises(RuntimeError, match="ownership"):
        await pools.effect_intent(pool, claim["id"], "owner-a", claim["epoch"], wrong, "create")
    await store.release_ownership()
    with pytest.raises(RuntimeError, match="ownership"):
        await pools.effect_intent(pool, claim["id"], "owner-a", claim["epoch"], fence, "create")
    row = await pools.observe(pool, request.execution_id)
    assert row["status"] == "leased"  # no create/start intent was accepted


async def test_two_controller_clients_race_one_shared_slot(ledger):
    import asyncio

    pools, fence = ledger
    pool = models().RunnerPoolProfile.model_validate({**profile().model_dump(), "slots": 1})
    await pools.submit(pool, invocation(pool, project=uuid4()))
    await pools.submit(pool, invocation(pool, project=uuid4()))
    claims = await asyncio.gather(pools.claim(pool, "owner-a", fence), pools.claim(pool, "owner-b", fence))
    assert sum(claim is not None for claim in claims) == 1


async def test_unstarted_expiry_can_reclaim_but_old_epoch_cannot_dispatch(ledger, store):
    pools, fence = ledger
    pool = profile()
    request = invocation(pool)
    await pools.submit(pool, request)
    first = await pools.claim(pool, "owner-a", fence)
    async with store._unit() as session:
        await session.execute(
            text("UPDATE forge_runner_invocations SET lease_expires_at=clock_timestamp()-interval '1 second'")
        )
    second = await pools.claim(pool, "owner-b", fence)
    assert second["id"] == first["id"] and second["epoch"] == first["epoch"] + 1
    with pytest.raises(RuntimeError, match="lease"):
        await pools.effect_intent(pool, first["id"], "owner-a", first["epoch"], fence, "create")
    await pools.effect_intent(pool, second["id"], "owner-b", second["epoch"], fence, "create")


async def test_start_is_one_shot_and_output_release_requires_drain(ledger):
    pools, fence = ledger
    pool = profile()
    request = invocation(pool)
    await pools.submit(pool, request)
    row = await pools.claim(pool, "owner-a", fence)
    args = pool, row["id"], "owner-a", row["epoch"], fence
    await pools.effect_intent(*args, "create")
    await pools.effect_receipt(*args, "create", {"container_id": "e" * 64})
    await pools.effect_intent(*args, "start")
    with pytest.raises(RuntimeError, match="replay"):
        await pools.effect_intent(*args, "start")
    with pytest.raises(ValueError, match="drained"):
        await pools.finish(*args, {"drained": False})
    await pools.effect_receipt(*args, "start", {"started": True})
    events = await pools.history(pool, request.execution_id)
    assert [event["kind"] for event in events][-4:] == [
        "create_intent",
        "create_receipt",
        "start_intent",
        "start_receipt",
    ]


async def test_waiting_cancel_and_timeout_do_not_consume_capacity(ledger, store):
    pools, fence = ledger
    pool = profile()
    one, two = invocation(pool), invocation(pool)
    await pools.submit(pool, one)
    await pools.submit(pool, two)
    await pools.cancel_waiting(pool, one.execution_id, fence)
    async with store._unit() as session:
        await session.execute(
            text("UPDATE forge_runner_invocations SET admission_expires_at=clock_timestamp()-interval '1 second'")
        )
    assert await pools.claim(pool, "owner-a", fence) is None
    assert (await pools.observe(pool, one.execution_id))["status"] == "cancelled"
    assert (await pools.observe(pool, two.execution_id))["status"] == "cancelled"


async def test_unknown_job_is_only_released_after_observed_reconciliation(ledger, store):
    pools, fence = ledger
    pool = profile()
    request = invocation(pool)
    await pools.submit(pool, request)
    row = await pools.claim(pool, "owner-a", fence)
    await pools.effect_intent(pool, row["id"], "owner-a", row["epoch"], fence, "create")
    await pools.quarantine(pool, row["id"], "owner-a", row["epoch"], {"reason": "transport disconnected"})
    with pytest.raises(ValueError, match="drained"):
        await pools.reconcile(pool, request.execution_id, fence, {"drained": False})
    await pools.reconcile(pool, request.execution_id, fence, {"drained": True, "outcome": "unknown"})
    assert (await pools.observe(pool, request.execution_id))["status"] == "finished"
    with pytest.raises(RuntimeError, match="lease"):
        await pools.authorize(pool, row["id"], "owner-a", row["epoch"], fence)


async def test_profile_revision_cannot_split_retained_capacity(ledger):
    pools, _ = ledger
    first = profile()
    await pools.submit(first, invocation(first))
    changed = models().RunnerPoolProfile.model_validate({**first.model_dump(), "slots": 3})
    with pytest.raises(RuntimeError, match="profile"):
        await pools.submit(changed, invocation(changed))


async def test_shared_advisory_lock_is_not_factory_execution_ownership(ledger, store):
    pools, old_fence = ledger
    pool = profile()
    await pools.submit(pool, invocation(pool))
    await store.release_ownership()
    connection = await store._engine.connect()
    try:
        await connection.execute(
            text("SELECT pg_advisory_lock_shared(hashtextextended(:key,0))"), {"key": "forge:execution:" + store.schema}
        )
        proof = (
            await connection.execute(text("SELECT pid,backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()"))
        ).one()
        await connection.commit()
        shared = models().FactoryFence(
            factory_schema=store.schema, pid=proof.pid, backend_started_at=proof.backend_start
        )
        with pytest.raises(RuntimeError, match="ownership"):
            await pools.claim(pool, "shared-owner", shared)
    finally:
        await connection.execute(
            text("SELECT pg_advisory_unlock_shared(hashtextextended(:key,0))"),
            {"key": "forge:execution:" + store.schema},
        )
        await connection.close()


async def test_one_physical_daemon_cannot_multiply_capacity_using_two_profile_ids(ledger):
    pools, fence = ledger
    first = profile()
    second = models().RunnerPoolProfile.model_validate({**first.model_dump(), "profile_id": "other-profile"})
    one = invocation(first)
    await pools.submit(first, one)
    await pools.claim(first, "owner-a", fence)
    with pytest.raises(RuntimeError, match="daemon"):
        await pools.submit(second, invocation(second, repository=one.repository_id))


async def test_new_profile_revision_preserves_old_requests_and_shares_capacity(ledger):
    pools, fence = ledger
    first = profile()
    old = invocation(first, project=uuid4())
    await pools.submit(first, old)
    a = await pools.claim(first, "owner-a", fence)
    changed = models().RunnerPoolProfile.model_validate({**first.model_dump(), "approved_images": [IMAGE, TRANSFER]})
    new = invocation(changed, project=uuid4())
    await pools.submit(changed, new)
    b = await pools.claim(changed, "owner-b", fence)
    assert a["request"] == old.model_dump(mode="json")
    assert b["request"] == new.model_dump(mode="json")
    await pools.submit(changed, invocation(changed, project=uuid4()))
    assert await pools.claim(changed, "owner-c", fence) is None
    assert (await pools.observe(first, old.execution_id))["request"] == old.model_dump(mode="json")
    await pools.finish(first, a["id"], "owner-a", a["epoch"], fence, {"drained": True})
    assert await pools.claim(changed, "owner-c", fence) is not None


async def test_controller_schema_is_distinct_and_leaves_factory_tables_and_version_unchanged(store, postgres_schema):
    from firefly_forge.runner_pools import RunnerPools

    runner_schema = postgres_schema + "_runner"
    async with store._unit() as session:
        before = (
            (
                await session.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname=:schema ORDER BY tablename"),
                    {"schema": postgres_schema},
                )
            )
            .scalars()
            .all()
        )
        version = await session.scalar(text("SELECT max(revision) FROM forge_schema_migrations"))
    pools = RunnerPools(store.session_provider, runner_schema, factory_schema=postgres_schema)
    try:
        await pools.initialize()
        await pools.submit(profile(), invocation(profile()))
        async with store._unit() as session:
            after = (
                (
                    await session.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname=:schema ORDER BY tablename"),
                        {"schema": postgres_schema},
                    )
                )
                .scalars()
                .all()
            )
            assert await session.scalar(text("SELECT max(revision) FROM forge_schema_migrations")) == version
            runner = (
                (
                    await session.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname=:schema"), {"schema": runner_schema}
                    )
                )
                .scalars()
                .all()
            )
        assert before == after and all(not name.startswith("forge_runner_") for name in after)
        assert set(runner) == {
            "forge_runner_pools",
            "forge_runner_invocations",
            "forge_runner_events",
            "forge_runner_daemons",
            "forge_runner_schema_migrations",
        }
    finally:
        async with store._unit() as session:
            await session.execute(text(f'DROP SCHEMA IF EXISTS "{runner_schema}" CASCADE'))


async def test_capacity_envelope_change_requires_all_old_requests_drained(ledger):
    pools, fence = ledger
    first = profile()
    request = invocation(first)
    await pools.submit(first, request)
    row = await pools.claim(first, "owner-a", fence)
    await pools.effect_intent(first, row["id"], "owner-a", row["epoch"], fence, "create")
    await pools.quarantine(first, row["id"], "owner-a", row["epoch"], {"reason": "unknown"})
    changed = models().RunnerPoolProfile.model_validate({**first.model_dump(), "slots": 3})
    with pytest.raises(RuntimeError, match="envelope"):
        await pools.submit(changed, invocation(changed))
    await pools.reconcile(first, request.execution_id, fence, {"drained": True, "outcome": "unknown"})
    await pools.submit(changed, invocation(changed))
    assert await pools.claim(changed, "owner-b", fence) is not None
    assert (await pools.observe(first, request.execution_id))["request"] == request.model_dump(mode="json")


async def test_revision_alias_cannot_create_second_repository_writer(ledger):
    pools, fence = ledger
    first = profile()
    request = invocation(first)
    await pools.submit(first, request)
    await pools.claim(first, "owner-a", fence)
    changed = models().RunnerPoolProfile.model_validate({**first.model_dump(), "approved_images": [IMAGE, TRANSFER]})
    await pools.submit(changed, invocation(changed, repository=request.repository_id, project=uuid4()))
    assert await pools.claim(changed, "owner-b", fence) is None


async def test_execution_identity_cannot_be_reused_under_another_revision_or_daemon(ledger):
    pools, _ = ledger
    first = profile()
    request = invocation(first)
    await pools.submit(first, request)
    for changes in (
        {"approved_images": [IMAGE, TRANSFER]},
        {"daemon_id": "other-engine", "profile_id": "other-profile"},
    ):
        changed = models().RunnerPoolProfile.model_validate({**first.model_dump(), **changes})
        duplicate = models().RunnerInvocation.model_validate(
            {**request.model_dump(), "profile_id": changed.profile_id, "profile_revision": changed.revision}
        )
        with pytest.raises(ValueError, match="immutable"):
            await pools.submit(changed, duplicate)
    assert (await pools.observe(first, request.execution_id))["request"] == request.model_dump(mode="json")


def test_optional_command_stdin_is_immutable_and_bounded():
    pool = profile()
    base = invocation(pool)
    value = models().RunnerInvocation.model_validate({**base.model_dump(), "stdin_bytes": 5, "stdin_digest": "f" * 64})
    assert value.stdin_bytes == 5
    for change in (
        {"stdin_bytes": 0, "stdin_digest": "f" * 64},
        {"stdin_bytes": 5, "stdin_digest": None},
        {"stdin_bytes": 2_097_153, "stdin_digest": "f" * 64},
    ):
        with pytest.raises(ValidationError):
            models().RunnerInvocation.model_validate({**base.model_dump(), **change})


async def test_new_factory_owner_cannot_start_old_queued_authorization(ledger, store):
    pools, fence = ledger
    pool = profile()
    request = invocation(pool)
    await pools.submit(pool, request, fence=fence)
    await store.release_ownership()
    await store.acquire_ownership()
    async with store._unit() as session:
        started = await session.scalar(
            text("SELECT backend_start FROM pg_stat_activity WHERE pid=:pid"), {"pid": store._ownership_pid}
        )
    new = models().FactoryFence(factory_schema=store.schema, pid=store._ownership_pid, backend_started_at=started)
    assert new != fence
    assert await pools.claim(pool, "new-controller", new) is None
    assert (await pools.observe(pool, request.execution_id))["status"] == "cancelled"
