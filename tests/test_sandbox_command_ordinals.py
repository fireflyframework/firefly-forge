# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Dedicated PostgreSQL qualification of command allocation; no VM or provider transport."""

import asyncio
from types import SimpleNamespace

import pytest
from test_runner_factory_authority import seed

from firefly_forge.journal import Journal, digest
from firefly_forge.process import ProcessResult
from firefly_forge.sandbox import SandboxExecutor, SandboxReconciliationRequired
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox


async def setup(store):
    run, journal = await seed(store)
    calls = []

    class Backend:
        async def run(self, config, argv, cwd, timeout, output_limit):
            from pyfly.data.transaction import current_unit_of_work

            assert current_unit_of_work() is None
            calls.append(argv)
            return ProcessResult(argv, 0, str(len(calls)))

    backends = SimpleNamespace(select=lambda config: Backend())
    return run, journal, calls, lambda: SandboxExecutor(store, Journal(store), backends)


@pytest.mark.parametrize("argv", [["same"], ["different"]])
async def test_fresh_service_allocates_new_native_slot(store, tmp_path, argv):
    run, journal, calls, fresh = await setup(store)
    first = await fresh().execute(run["id"], tmp_path, tmp_path, ["same"], 2, "verify")
    second = await fresh().execute(run["id"], tmp_path, tmp_path, argv, 2, "verify")
    assert [first.output, second.output] == ["1", "2"]
    assert [row["ordinal"] for row in await journal.sandbox_executions(run["id"])] == [1, 2]
    assert Journal.verify_chain(await journal.entries(run["id"]))


async def test_concurrent_native_writers_commit_one_create_before_another_dispatch(store, tmp_path):
    run, journal, calls, fresh = await setup(store)
    snapshot = run["sandbox_snapshot"]

    def payload(number, identity, previous):
        request = {"argv": ["controlled"], "configuration": snapshot["configuration"]}
        return {
            "parent_attempt": 1,
            "ordinal": number,
            "container_name": "forge-sandbox-" + identity,
            "snapshot_digest": snapshot["digest"],
            "request_digest": digest(request),
            "request": request,
        }

    async def claim():
        return await Journal(store).sandbox_command_intent(run["id"], "verify", 1, snapshot, payload)

    values = await asyncio.gather(claim(), claim(), return_exceptions=True)
    assert sum(isinstance(value, dict) for value in values) == 1
    assert sum(isinstance(value, SandboxReconciliationRequired) for value in values) == 1
    assert len(await journal.sandbox_executions(run["id"])) == 1 and calls == []
    assert Journal.verify_chain(await journal.entries(run["id"]))


async def test_native_explicit_replay_does_not_dispatch_twice(store, tmp_path):
    run, journal, calls, fresh = await setup(store)
    first = await fresh().execute(run["id"], tmp_path, tmp_path, ["same"], 2, "verify")
    assert await fresh().execute(run["id"], tmp_path, tmp_path, ["same"], 2, "verify", ordinal=1) == first
    with pytest.raises(ValueError, match="changed"):
        await fresh().execute(run["id"], tmp_path, tmp_path, ["different"], 2, "verify", ordinal=1)
    assert len(calls) == 1


async def test_native_global_budget_survives_fresh_service_and_next_attempt(store, tmp_path):
    run, journal, calls, fresh = await setup(store)
    await store.update(run["id"], sandbox_snapshot=capture_sandbox(SandboxInput(command_limit=1)))
    await fresh().execute(run["id"], tmp_path, tmp_path, ["same"], 2, "verify")
    async with store._unit():
        task = await journal._task(run["id"], "verify")
        task.attempt += 1
        await journal.tasks.save(task)
    with pytest.raises(ValueError, match="total command budget"):
        await fresh().execute(run["id"], tmp_path, tmp_path, ["different"], 2, "verify")
    assert len(calls) == 1 and len(await journal.sandbox_executions(run["id"])) == 1


async def test_native_pool_fresh_service_and_explicit_replay_share_the_allocator(store, tmp_path):
    from test_runner_pool_worker import fixture

    from firefly_forge.runner_pool_worker import RunnerPoolWorker

    worker, controller, run, journal, workspace = await fixture(store, tmp_path)

    def fresh():
        executor = SandboxExecutor(store, Journal(store))
        executor.pool_worker = RunnerPoolWorker(
            store,
            Journal(store),
            worker.authority,
            controller,
            worker.git_snapshot,
            worker.workspaces,
            worker.artifacts.directory,
        )
        return executor

    first = await fresh().execute(run["id"], workspace, workspace, ["same"], 2, "verify")
    second = await fresh().execute(run["id"], workspace, workspace, ["same"], 2, "verify")
    assert first.exit_code == second.exit_code == 0 and controller.submissions == 2
    assert await fresh().execute(run["id"], workspace, workspace, ["same"], 2, "verify", ordinal=1) == first
    assert controller.submissions == 2
    assert [row["ordinal"] for row in await journal.sandbox_executions(run["id"])] == [1, 2]
