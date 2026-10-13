# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio

import pytest

from firefly_forge.models import RepositoryInput, RunInput


async def test_postgres_claim_and_terminal_event_are_atomic(tmp_path, store_factory):
    first = await store_factory(tmp_path / "state.db")
    second = await store_factory(tmp_path / "state.db")
    repo = await first.add_repository(RepositoryInput(name="demo", url="https://github.com/example/demo.git"))
    run = await first.create_run(RunInput(repository_id=repo["id"], brief="Create service"))
    assert sum(await asyncio.gather(*(store.claim(run["id"]) for store in [first, second]))) == 1
    await asyncio.gather(first.finish(run["id"], "succeeded"), second.finish(run["id"], "cancelled"))
    result = await first.get_run(run["id"])
    terminal = [event for event in result["events"] if event["stage"] in {"succeeded", "cancelled"}]
    assert len(terminal) == 1
    assert terminal[0]["stage"] == result["status"]
    assert not (tmp_path / "state.db").exists()


async def test_deployment_exclusivity_recovery_and_explicit_retry(tmp_path, store_factory):
    first = await store_factory(tmp_path / "state.db")
    second = await store_factory(tmp_path / "state.db")
    repo = await first.add_repository(RepositoryInput(name="demo", url="https://github.com/example/demo.git"))
    run = await first.create_run(RunInput(repository_id=repo["id"], brief="Create service"))
    data = {"run_id": run["id"], "target": "staging", "provider": "local"}
    results = await asyncio.gather(
        first.create_deployment(data), second.create_deployment(data), return_exceptions=True
    )
    created = [result for result in results if isinstance(result, dict)]
    rejected = [result for result in results if isinstance(result, ValueError)]
    assert len(created) == len(rejected) == 1
    identifier = created[0]["id"]
    assert sum(await asyncio.gather(first.claim_deployment(identifier), second.claim_deployment(identifier))) == 1
    assert await second.recover_deployments() == 1
    assert (await first.get_deployment(identifier))["status"] == "interrupted"
    assert await first.recover_deployments() == 0
    retry = await first.create_deployment(data)
    assert retry["id"] != identifier
    assert len(await first.deployments(run["id"])) == 2
    assert retry["status"] == "queued"


async def test_failed_terminal_transition_rolls_back_both_state_and_event(tmp_path, store_factory, monkeypatch):
    store = await store_factory(tmp_path / "state.db")
    repo = await store.add_repository(RepositoryInput(name="demo", url="https://github.com/example/demo.git"))
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Create service"))
    await store.claim(run["id"])
    from firefly_forge.persistence import ForgeEventRepository

    async def fail(*args, **kwargs):
        raise RuntimeError("event storage failed")

    monkeypatch.setattr(ForgeEventRepository, "save", fail)
    with pytest.raises(RuntimeError, match="event storage"):
        await store.finish(run["id"], "failed")
    result = await store.get_run(run["id"])
    assert result["status"] == "preparing"
    assert [event["stage"] for event in result["events"]] == ["queued"]


def test_store_supports_nonblocking_database_startup():
    import inspect

    from firefly_forge.store import Store

    assert inspect.iscoroutinefunction(Store.claim), "Database queue claims must be awaitable"
    assert inspect.iscoroutinefunction(Store.finish)
    assert hasattr(Store, "initialize"), "Database setup runs in the application's async startup"


async def test_concurrent_recovery_counts_each_interrupted_run_once(tmp_path, store_factory):
    first = await store_factory(tmp_path / "state.db")
    second = await store_factory(tmp_path / "state.db")
    repo = await first.add_repository(RepositoryInput(name="demo", url="https://github.com/example/demo.git"))
    run = await first.create_run(RunInput(repository_id=repo["id"], brief="Create service"))
    await first.claim(run["id"])
    recovered = await asyncio.gather(first.recover(), second.recover())
    assert sum(recovered) == 1
    result = await first.get_run(run["id"])
    assert [event["stage"] for event in result["events"]] == ["queued", "interrupted"]


async def test_store_lifecycle_prepares_database_before_consumers(tmp_path, postgres_schema):
    from firefly_forge.store import Store

    store = Store(tmp_path / "state.db")
    try:
        await store.start()
        repo = await store.add_repository(RepositoryInput(name="demo", url="https://github.com/example/demo.git"))
        assert (await store.repository(repo["id"]))["name"] == "demo"
        await store.stop()
        with pytest.raises(RuntimeError, match="initialize"):
            await store.repositories()
    finally:
        await store.close()


async def test_write_ahead_transactions_override_async_commit_on_their_own_connection(store):
    from sqlalchemy import text

    async with store._unit() as session:
        await session.execute(text("SET SESSION synchronous_commit TO off"))
    async with store._unit() as session:
        assert await session.scalar(text("SELECT current_setting('synchronous_commit')")) == "on"


async def test_deployment_dispatch_reads_oldest_queued_records_with_a_bound(store):
    repo = await store.add_repository(RepositoryInput(name="demo", url="https://github.com/example/demo.git"))
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Create service"))
    first = await store.create_deployment({"run_id": run["id"], "target_id": "first"})
    second = await store.create_deployment({"run_id": run["id"], "target_id": "second"})
    active = await store.create_deployment({"run_id": run["id"], "target_id": "active"})
    await store.claim_deployment(active["id"])
    assert [record["id"] for record in await store.queued_deployments(limit=1)] == [first["id"]]
    assert [record["id"] for record in await store.queued_deployments()] == [first["id"], second["id"]]


async def test_database_ownership_excludes_other_data_directories_until_release(tmp_path, store_factory):
    first = await store_factory(tmp_path / "first" / "state.db")
    second = await store_factory(tmp_path / "second" / "state.db")
    await first.acquire_ownership()
    await first.assert_ownership()
    with pytest.raises(RuntimeError, match="already owns"):
        await second.acquire_ownership()
    await first.release_ownership()
    await second.acquire_ownership()
    await second.assert_ownership()
    await second.release_ownership()


async def test_database_ownership_detects_a_lost_lock(store):
    from sqlalchemy import text

    await store.acquire_ownership()
    await store._ownership_connection.execute(text("SELECT pg_advisory_unlock_all()"))
    with pytest.raises(RuntimeError, match="ownership.*lost"):
        await store.assert_ownership()
    await store.release_ownership()


async def test_concurrent_poll_and_stage_checks_share_the_ownership_connection(store):
    await store.acquire_ownership()
    await asyncio.gather(*(store.assert_ownership() for _ in range(10)))
    await store.release_ownership()
