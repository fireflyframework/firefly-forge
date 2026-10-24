# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real capture/creation/Journal/SandboxExecutor with memory rows and a controlled backend."""

import copy
from types import MethodType
from uuid import UUID

import pytest
from test_automatic_verification_engine_host import SandboxRows
from test_bridge_inflight_cancel import MemoryStore, Rows
from test_run_admission_prepared_host import REPOSITORY
from test_run_admission_prepared_host import admission as admission
from test_runner_configuration import service

from firefly_forge.journal import Journal
from firefly_forge.models import RunInput
from firefly_forge.process import ProcessResult
from firefly_forge.sandbox import SandboxExecutor
from firefly_forge.sandbox_models import SandboxInput, validate_sandbox_snapshot
from firefly_forge.store import Store


async def test_creation_capture_persists_seven_and_executor_refuses_eighth_or_reset_attempt(admission, tmp_path):
    store, state, _, _ = admission
    store.runners = await service(store, tmp_path)
    capture = store.runners.capture_run
    captures = []

    async def captured(repository, selected):
        assert state["depth"] == 0
        value = await capture(repository, selected)
        captures.append(copy.deepcopy(value))
        return value

    store.runners.capture_run = captured
    commands = [{"argv": ["public-check", str(i)], "timeout": 30} for i in range(1, 8)]
    child = await store.create_run(
        RunInput(
            repository_id=REPOSITORY,
            operation="test",
            publish=False,
            brief="Verify seven explicitly configured public checks",
            commands=commands,
            sandbox=SandboxInput(mode="host", network="none", command_limit=7),
        )
    )
    assert len(captures) == 1
    assert state["rows"][child["id"]]["sandbox_snapshot"] == captures[0]["sandbox_snapshot"]
    assert child["commands"] == commands and validate_sandbox_snapshot(child["sandbox_snapshot"]).command_limit == 7
    tampered = copy.deepcopy(child["sandbox_snapshot"])
    tampered["configuration"]["command_limit"] = 8
    with pytest.raises(ValueError, match="digest"):
        validate_sandbox_snapshot(tampered)

    memory = MemoryStore(tmp_path / "memory" / "factory.db")
    memory.schema = "host_seven_commands"
    memory.configuration = store.configuration
    memory.assert_campaign_current = MethodType(Store.assert_campaign_current, memory)
    memory.run.id = UUID(child["id"])
    memory.run.data = copy.deepcopy(child)
    memory.run.status = child["status"]
    ownership = []

    async def owned():
        ownership.append(True)

    memory.assert_ownership = owned
    journal = Journal(memory, plans=Rows(), tasks=Rows(), records=Rows(), sandboxes=SandboxRows())
    await journal.ensure_plan(child["id"])
    stage = await journal.intent(child["id"], "verify")
    calls = []

    class Backend:
        async def run(self, config, argv, cwd, timeout, limit):
            assert config.command_limit == 7 and argv in [c["argv"] for c in commands]
            calls.append(list(argv))
            return ProcessResult(argv, 0, "Controlled public verification result")

    class Backends:
        def select(self, config):
            return Backend()

    executor = SandboxExecutor(memory, journal, backends=Backends())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for ordinal, command in enumerate(commands, 1):
        result = await executor.execute(
            child["id"], workspace, workspace, command["argv"], command["timeout"], "verify", ordinal=ordinal
        )
        assert result.exit_code == 0
    receipts = await journal.sandbox_executions(child["id"])
    assert calls == [c["argv"] for c in commands] and len(receipts) == 7
    assert sorted(row["ordinal"] for row in receipts) == list(range(1, 8))
    assert all(row["effects"]["start"]["status"] == "done" for row in receipts)
    with pytest.raises(ValueError, match="command budget is exhausted"):
        await executor.execute(child["id"], workspace, workspace, ["public-check", "8"], 30, "verify", ordinal=8)
    await journal.fail(child["id"], stage["idempotency_key"], "quota", "Public exhausted command budget")
    await journal.resume(child["id"])
    resumed = await journal.intent(child["id"], "verify", attempt=2)
    assert resumed["disposition"] == "execute"
    assert next(t for t in (await journal.plan(child["id"]))["tasks"] if t["id"] == "verify")["attempt"] == 2
    with pytest.raises(ValueError, match="total command budget is exhausted"):
        await SandboxExecutor(memory, journal, backends=Backends()).execute(
            child["id"], workspace, workspace, commands[0]["argv"], 30, "verify", ordinal=1
        )
    assert len(calls) == 7 and await journal.sandbox_executions(child["id"]) == receipts and ownership
