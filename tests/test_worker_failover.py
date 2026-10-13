# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Standbys cannot recover a live owner's work or turn contention into extra capacity."""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from firefly_forge.engine import Engine
from firefly_forge.models import Command, RepositoryInput, RunInput
from firefly_forge.runtime_roles import RuntimeRoles
from firefly_forge.worker_runtime import WorkerSupervision, run_worker


async def worker_engine(store, root):
    store.runtime_roles = RuntimeRoles(store, "worker")
    await store.runtime_roles.start()
    return Engine(store, root / "workspaces")


async def wait_for(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not await predicate():
            await asyncio.sleep(0.02)


async def test_standby_does_not_recover_live_work_and_promotes_after_owner_drains(tmp_path, store_factory):
    first_store = await store_factory(tmp_path / "factory.db")
    second_store = await store_factory(tmp_path / "factory.db")
    first = await worker_engine(first_store, tmp_path)
    second = await worker_engine(second_store, tmp_path)
    await first.start()
    repository = await first_store.add_repository(RepositoryInput(name="fixture", url="/unused-fixture"))
    run = await first_store.create_run(RunInput(repository_id=repository["id"], brief="inflight fixture"))
    assert await first_store.claim(run["id"])
    starting = asyncio.create_task(second.start())
    try:
        await asyncio.sleep(0.1)
        assert not starting.done(), "A split worker must remain available as a standby"
        assert second.execution_state == "standby"
        health = second_store.runtime_roles.health(execution_state=second.execution_state, max_parallel_jobs=2)
        assert not health["execution_ready"] and health["execution"]["max_parallel_jobs"] == 0
        assert WorkerSupervision(second, {"execution": second}).detail() is None
        assert second_store._ownership_connection is None
        with pytest.raises(RuntimeError, match="ownership was lost"):
            await second.dispatch_queued()
        assert (await first_store.get_run(run["id"]))["status"] == "preparing"
        await first.stop()
        await asyncio.wait_for(starting, 5)
        assert second.execution_state == "ready"
        await second_store.assert_ownership()
        assert (await second_store.get_run(run["id"]))["status"] == "interrupted"
    finally:
        starting.cancel()
        await asyncio.gather(starting, return_exceptions=True)
        await second.stop()
        await first.stop()


async def test_standby_waits_for_postgres_contention_without_holding_file_fence(tmp_path, store_factory):
    first = await store_factory(tmp_path / "factory.db")
    second_store = await store_factory(tmp_path / "factory.db")
    second = await worker_engine(second_store, tmp_path)
    await first.acquire_ownership()
    starting = asyncio.create_task(second.start())
    try:
        await asyncio.sleep(0.1)
        assert not starting.done()
        assert not second.owner_lock.is_locked
        assert second_store._ownership_connection is None
        await first.release_ownership()
        await asyncio.wait_for(starting, 5)
        await second_store.assert_ownership()
    finally:
        starting.cancel()
        await asyncio.gather(starting, return_exceptions=True)
        await second.stop()
        await first.release_ownership()


async def test_recovery_rechecks_fence_between_services_and_cleans_failed_start(store, tmp_path):
    engine = await worker_engine(store, tmp_path)
    repository = await store.add_repository(RepositoryInput(name="fixture", url="/unused-fixture"))
    run = await store.create_run(RunInput(repository_id=repository["id"], brief="inflight fixture"))
    assert await store.claim(run["id"])

    class InterruptedJournal:
        async def recover(self):
            await store.release_ownership()

    engine.journal = InterruptedJournal()
    with pytest.raises(RuntimeError, match="ownership was lost"):
        await engine.start()
    assert (await store.get_run(run["id"]))["status"] == "preparing"
    assert not engine.owner_lock.is_locked
    assert engine.poller is None
    await engine.stop()


async def test_worker_does_not_retry_database_errors(store, tmp_path, monkeypatch):
    engine = await worker_engine(store, tmp_path)
    calls = 0

    async def unavailable():
        nonlocal calls
        calls += 1
        raise ConnectionError("test database unavailable")

    monkeypatch.setattr(store, "acquire_ownership", unavailable)
    with pytest.raises(ConnectionError, match="database unavailable"):
        await asyncio.wait_for(engine.start(), 1)
    assert calls == 1 and not engine.owner_lock.is_locked
    await engine.stop()


async def test_presignalled_worker_never_attempts_leadership(monkeypatch, tmp_path):
    import firefly_forge.api as api

    def forbidden(*args, **kwargs):
        pytest.fail("Stopped worker attempted startup")

    monkeypatch.setattr(api, "create_runtime", forbidden)
    stop = asyncio.Event()
    stop.set()
    await run_worker(tmp_path, stop=stop)


async def test_stop_during_acquisition_never_recovers_or_retains_leadership(store, tmp_path, monkeypatch):
    engine = await worker_engine(store, tmp_path)
    repository = await store.add_repository(RepositoryInput(name="fixture", url="/unused-fixture"))
    run = await store.create_run(RunInput(repository_id=repository["id"], brief="inflight fixture"))
    assert await store.claim(run["id"])
    started, continue_acquisition = asyncio.Event(), asyncio.Event()
    acquire = store.acquire_ownership

    async def delayed_acquisition():
        started.set()
        await continue_acquisition.wait()
        await acquire()

    monkeypatch.setattr(store, "acquire_ownership", delayed_acquisition)
    starting = asyncio.create_task(engine.start())
    await started.wait()
    await engine.stop()
    continue_acquisition.set()
    await asyncio.wait_for(starting, 2)
    try:
        assert engine.poller is None and engine.execution_state == "stopped"
        assert store._ownership_connection is None and not engine.owner_lock.is_locked
        assert (await store.get_run(run["id"]))["status"] == "preparing"
        with pytest.raises(RuntimeError, match="ownership was lost"):
            await engine.dispatch_queued()
    finally:
        await engine.stop()


async def test_ownership_loss_retains_storage_fence_until_lifecycle_consumers_drain(store, tmp_path):
    engine = await worker_engine(store, tmp_path)
    await engine.start()
    try:
        await store.release_ownership()
        await asyncio.wait_for(engine.poller, 2)
        assert engine.execution_state == "degraded"
        assert engine.owner_lock.is_locked, "A standby must not recover while other native consumers are draining"
        with pytest.raises(RuntimeError, match="ownership was lost"):
            await engine.dispatch_queued()
    finally:
        await engine.stop()
    assert not engine.owner_lock.is_locked


@pytest.mark.parametrize("concurrency", [0, -1, 65])
async def test_worker_concurrency_is_a_bounded_execution_contract(store, tmp_path, concurrency):
    with pytest.raises(ValueError, match="concurrency"):
        Engine(store, tmp_path / "workspaces", concurrency=concurrency)


@asynccontextmanager
async def headless_process(root, name, *, release=None):
    environment = dict(os.environ)
    environment.update(
        FORGE_AUTH_MODE="local_bootstrap",
        FORGE_API_TOKEN="headless-worker-local-fixture-token",
        FORGE_CONCURRENCY="2",
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
    )
    with (root / f"{name}.log").open("wb") as logs:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "from firefly_forge.cli import main; main()",
            "worker",
            "--data-dir",
            str(root / "data"),
            env=environment,
            stdout=logs,
            stderr=logs,
        )
        try:
            yield process
        finally:
            if release is not None:
                release.touch()
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except TimeoutError:
                    process.kill()
                    await process.wait()


async def owner_pids(store):
    from sqlalchemy import text

    async with store._unit() as session:
        rows = await session.scalars(
            text(
                "SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted "
                "AND database=(SELECT oid FROM pg_database WHERE datname=current_database()) "
                "AND objsubid=1 AND classid::bigint=((hashtextextended(:key,0)>>32)&4294967295) "
                "AND objid::bigint=(hashtextextended(:key,0)&4294967295)"
            ),
            {"key": "forge:execution:" + store.schema},
        )
        return list(rows)


async def wait_for_owner(store, previous=None):
    async def changed():
        pids = await owner_pids(store)
        return len(pids) == 1 and pids != previous

    await wait_for(changed, 15)
    return await owner_pids(store)


async def wait_for_run(store, run_id, statuses):
    async def terminal():
        return (await store.get_run(run_id))["status"] in statuses

    await wait_for(terminal, 15)
    return await store.get_run(run_id)


async def test_real_headless_standby_stops_without_taking_leadership(tmp_path, store_factory):
    store = await store_factory(tmp_path / "data" / "factory.db")
    store.runtime_roles = RuntimeRoles(store, "api")
    await store.runtime_roles.start()
    async with headless_process(tmp_path, "leader") as leader:
        original = await wait_for_owner(store)
        async with headless_process(tmp_path, "standby") as standby:
            await asyncio.sleep(1.5)
            assert standby.returncode is None, "Standby exited while another worker owned execution"
            assert await owner_pids(store) == original
            standby.terminate()
            assert await asyncio.wait_for(standby.wait(), 10) == 0
            assert leader.returncode is None and await owner_pids(store) == original


@pytest.mark.parametrize("failure", ["process_crash", "database_fence_loss"])
async def test_real_headless_failover_never_replays_an_unreceipted_command(tmp_path, store_factory, failure):
    from test_execution import seed_repository

    from firefly_forge.journal import Journal

    store = await store_factory(tmp_path / "data" / "factory.db")
    store.runtime_roles = RuntimeRoles(store, "api")
    await store.runtime_roles.start()
    source = await seed_repository(tmp_path / "source")
    repository = await store.add_repository(RepositoryInput(name="fixture", url=str(source)))
    marker = tmp_path / "effect.txt"
    next_marker = tmp_path / "after-failover.txt"
    release = tmp_path / "release-inflight"
    async with headless_process(tmp_path, "leader", release=release) as leader:
        original = await wait_for_owner(store)
        interrupted = await store.create_run(
            RunInput(
                repository_id=repository["id"],
                brief="Execute a retained verification fixture",
                operation="test",
                commands=[
                    Command(
                        argv=[
                            sys.executable,
                            "-c",
                            "from pathlib import Path; import time\n"
                            f"Path({str(marker)!r}).open('a').write('effect\\n')\n"
                            "deadline = time.monotonic() + 30\n"
                            f"while not Path({str(release)!r}).exists() and time.monotonic() < deadline:\n"
                            " time.sleep(.02)\n",
                        ]
                    )
                ],
            )
        )

        async def effect_started():
            current = await store.get_run(interrupted["id"])
            assert current["status"] not in {"failed", "succeeded"}, current.get("error")
            return marker.exists()

        await wait_for(effect_started, 15)
        async with headless_process(tmp_path, "standby") as standby:
            await asyncio.sleep(1.1)
            assert standby.returncode is None and await owner_pids(store) == original
            assert (await store.get_run(interrupted["id"]))["status"] == "validating"
            queued = await store.create_run(
                RunInput(
                    repository_id=repository["id"],
                    brief="Check source after promotion",
                    operation="test",
                    commands=[
                        Command(
                            argv=[
                                sys.executable,
                                "-c",
                                f"from pathlib import Path; Path({str(next_marker)!r}).open('a').write('effect\\n')",
                            ]
                        )
                    ],
                )
            )
            if failure == "process_crash":
                leader.kill()
                await leader.wait()
            else:
                from sqlalchemy import text

                async with store._unit() as session:
                    assert await session.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": original[0]})
            promoted = await wait_for_owner(store, original)
            assert promoted != original and len(promoted) == 1
            result = await wait_for_run(store, queued["id"], {"succeeded", "failed"})
            assert result["status"] == "succeeded", result.get("error")
            expected = "needs_reconciliation" if failure == "process_crash" else "interrupted"
            assert (await store.get_run(interrupted["id"]))["status"] == expected
            assert marker.read_text() == "effect\n" and next_marker.read_text() == "effect\n"
            plan = await Journal(store).plan(interrupted["id"])
            assert next(task for task in plan["tasks"] if task["id"] == "verify")["status"] == "needs_reconciliation"
            assert Journal.verify_chain(await Journal(store).entries(interrupted["id"]))
            assert standby.returncode is None and len(await owner_pids(store)) == 1


async def test_two_headless_processes_keep_one_bounded_parallel_job_budget(tmp_path, store_factory):
    from test_execution import seed_repository

    store = await store_factory(tmp_path / "data" / "factory.db")
    store.runtime_roles = RuntimeRoles(store, "api")
    await store.runtime_roles.start()
    release = tmp_path / "release-commands"
    runs, markers = [], []
    for index in range(3):
        source = await seed_repository(tmp_path / f"source-{index}")
        repository = await store.add_repository(RepositoryInput(name=f"fixture-{index}", url=str(source)))
        marker = tmp_path / f"effect-{index}"
        markers.append(marker)
        runs.append(
            await store.create_run(
                RunInput(
                    repository_id=repository["id"],
                    brief="Check bounded parallel execution",
                    operation="test",
                    commands=[
                        Command(
                            argv=[
                                sys.executable,
                                "-c",
                                "from pathlib import Path; import time\n"
                                f"Path({str(marker)!r}).open('a').write('effect\\n')\n"
                                f"while not Path({str(release)!r}).exists():\n time.sleep(.02)\n",
                            ]
                        )
                    ],
                )
            )
        )
    async with headless_process(tmp_path, "leader"), headless_process(tmp_path, "standby") as standby:
        await wait_for_owner(store)

        async def two_started():
            for run in runs:
                current = await store.get_run(run["id"])
                assert current["status"] != "failed", current.get("error")
            return sum(marker.exists() for marker in markers) == 2

        try:
            await wait_for(two_started, 15)
            await asyncio.sleep(0.5)
            assert sum(marker.exists() for marker in markers) == 2
            states = [(await store.get_run(run["id"]))["status"] for run in runs]
            assert states.count("queued") == 1
            assert standby.returncode is None and len(await owner_pids(store)) == 1
        finally:
            release.touch()
        for run, marker in zip(runs, markers, strict=True):
            result = await wait_for_run(store, run["id"], {"succeeded", "failed"})
            assert result["status"] == "succeeded", result.get("error")
            assert marker.read_text() == "effect\n"
