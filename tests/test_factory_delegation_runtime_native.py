# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owned native rows support controlled parallel Agentic children and durable fan-in."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fireflyframework_agentic.agents import FireflyAgent
from fireflyframework_agentic.config import FireflyAgenticConfig
from pydantic_ai import PromptedOutput
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import UsageLimits
from test_factory_delegation_admission_native import seed

from firefly_forge.delegation_agent_budget import FamilyAgentCapability
from firefly_forge.delegation_lifecycle import ChildResult
from firefly_forge.delegation_models import DelegationJoinInput, DelegationStartInput, DelegationWaitInput
from firefly_forge.engine import Engine
from firefly_forge.factory_delegations import FactoryDelegations
from firefly_forge.journal import Journal
from firefly_forge.workspace_tools import WorkspaceTools


async def test_native_parallel_agents_wait_reacquire_and_join_preserve_original_family(store, tmp_path):
    session, manager, source = await seed(store, tmp_path)
    (source / "untracked.txt").write_text("Parent untracked source\n")
    admissions = [
        await session.start(DelegationStartInput(request_id=value * 32, task_id=task, brief=task))
        for value, task in [("a", "first"), ("b", "second")]
    ]
    ids = [item["delegation_id"] for item in admissions]
    entered, closed, model_ids = set(), set(), set()
    both, release = asyncio.Event(), asyncio.Event()
    parent_before = await store.get_run(session.run_id)

    async def worker(run):
        capability = FamilyAgentCapability(manager, session, run["id"])
        tools = WorkspaceTools(Path(run["factory_context"]["source"]["path"]))
        turns = 0

        async def respond(messages, info):
            nonlocal turns
            turns += 1
            if turns == 1:
                entered.add(run["id"])
                if len(entered) == 2:
                    both.set()
                await release.wait()
                return ModelResponse(parts=[ToolCallPart("read_file", {"path": "untracked.txt"}, run["id"])])
            assert "Parent untracked source" in repr(messages)
            return ModelResponse(
                parts=[TextPart(json.dumps({"summary": run["brief"], "changes": [], "delivery_verified": False}))]
            )

        @asynccontextmanager
        async def model_scope():
            model = FunctionModel(respond, model_name=run["id"])
            model_ids.add(id(model))
            try:
                yield model
            finally:
                closed.add(run["id"])

        try:
            async with model_scope() as model:
                agent = FireflyAgent(
                    "fictional-native-child",
                    model=model,
                    output_type=PromptedOutput(ChildResult),
                    capabilities=[capability],
                    tools=[tools.read_file],
                    auto_register=False,
                    retries=0,
                    config=FireflyAgenticConfig(default_temperature=None, rate_limit_max_retries=0),
                )
                result = await agent.run(
                    run["brief"], timeout=None, usage_limits=UsageLimits(request_limit=None, tool_calls_limit=None)
                )
                return result.output.model_dump(mode="json")
        finally:
            await capability.close()

    manager.runtime.worker = worker
    engine = Engine(store, tmp_path / "workspaces", concurrency=2)
    engine.factory_delegations = manager
    waiting = asyncio.create_task(session.wait(DelegationWaitInput(request_id="c" * 32, delegation_ids=ids)))
    engine.active[session.run_id] = waiting
    engine.active_projects[session.run_id] = parent_before["repository_id"]
    try:
        for _ in range(100):
            if manager.runtime.waits:
                break
            await asyncio.sleep(0.01)
        assert manager.runtime.waits
        await engine.dispatch_queued()
        await asyncio.wait_for(both.wait(), 5)
        assert len(engine.active) == 3 and len(model_ids) == 2 and not waiting.done()
        release.set()
        await asyncio.wait_for(
            asyncio.gather(*(task for identity, task in engine.active.items() if identity != session.run_id)), 10
        )
        for _ in range(100):
            if next(iter(manager.runtime.waits.values())).stage == "reacquiring":
                break
            await asyncio.sleep(0.01)
        assert not waiting.done()
        await engine.dispatch_queued()
        result = await asyncio.wait_for(waiting, 5)
        assert result["outcome"] == "satisfied" and result["resumed"]
        joined = await session.join(DelegationJoinInput(request_id="d" * 32, delegation_ids=list(reversed(ids))))
        assert joined["status"] == "ready" and not joined["applied"] and not joined["delivery_verified"]
        assert [child["task_id"] for child in joined["children"]] == ["first", "second"]
        state = await manager.ledger.state(session.run_id)
        assert state["charged"] == {"requests": 4, "tool_calls": 2, "commands": 0, "broker_calls": 0}
        assert state["unknown"] == dict.fromkeys(state["unknown"], 0)
        assert closed == entered == set(ids)
        assert (source / "untracked.txt").read_text() == "Parent untracked source\n"
        current = await store.get_run(session.run_id)
        for field in ["attempt", "work_duration", "sandbox_snapshot", "model_selection", "factory_team"]:
            assert current.get(field) == parent_before.get(field)
        rows = await manager.journal.entries(session.run_id)
        assert Journal.verify_chain(rows)
        assert len([row for row in rows if row["kind"] == "intent" and row["task_id"] == "implement"]) == 1
        assert not any(row["kind"] == "receipt" for row in rows)
    finally:
        release.set()
        for task in engine.active.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*engine.active.values(), return_exceptions=True)


async def test_native_spent_unknown_child_cannot_replay_after_manager_restart(store, tmp_path):
    session, manager, _ = await seed(store, tmp_path)
    admitted = await session.start(DelegationStartInput(request_id="a" * 32, task_id="unknown", brief="Observe"))
    calls = 0

    async def worker(run):
        nonlocal calls
        calls += 1
        key = "controlled-model-dispatch"
        await manager.ledger.reserve(session.run_id, run["id"], key, "requests")
        await manager.ledger.observe(session.run_id, key, "budget_dispatched")
        await manager.ledger.observe(session.run_id, key, "budget_unknown")
        raise RuntimeError("Controlled response loss")

    manager.runtime.worker = worker
    result = await manager.runtime.execute_child(admitted["delegation_id"])
    assert result["state"] == "needs_reconciliation" and calls == 1
    restarted = FactoryDelegations(store, manager.journal, sources=manager.sources, readiness=manager.readiness)
    resumed = restarted.bind(session.run_id, session.task_key, session.workspace)
    restarted.runtime.worker = worker
    with pytest.raises(ValueError):
        await restarted.runtime.execute_child(admitted["delegation_id"])
    assert (await restarted.observe(resumed, [admitted["delegation_id"]]))["children"][0] == result
    request = DelegationWaitInput(request_id="b" * 32, delegation_ids=[admitted["delegation_id"]])
    waiting = asyncio.create_task(resumed.wait(request))
    for _ in range(100):
        if restarted.runtime.waits and next(iter(restarted.runtime.waits.values())).stage == "reacquiring":
            break
        await asyncio.sleep(0.01)
    assert await restarted.reacquire(1) == 1
    observed = await asyncio.wait_for(waiting, 3)
    assert observed["outcome"] == "unavailable" and observed["children"][0]["state"] == "needs_reconciliation"
    assert (await restarted.ledger.state(session.run_id))["unknown"]["requests"] == 1 and calls == 1


async def test_native_repeated_cancellation_never_settles_before_owned_child_cleanup(store, tmp_path):
    session, manager, _ = await seed(store, tmp_path)
    admitted = await session.start(DelegationStartInput(request_id="a" * 32, task_id="cancel", brief="Observe"))
    entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def worker(run):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()

    manager.runtime.worker = worker
    execution = asyncio.create_task(manager.execute_child(admitted["delegation_id"]))
    await asyncio.wait_for(entered.wait(), 2)
    execution.cancel()
    await asyncio.wait_for(cleanup.wait(), 2)
    execution.cancel()
    await asyncio.sleep(0.03)
    try:
        assert not execution.done()
        assert (await manager.observe(session, [admitted["delegation_id"]]))["children"][0]["state"] == "running"
    finally:
        release.set()
        await asyncio.gather(execution, return_exceptions=True)
    observed = (await manager.observe(session, [admitted["delegation_id"]]))["children"][0]
    assert observed["state"] == "cancelled" and not observed["summary"]
    assert Journal.verify_chain(await manager.journal.entries(session.run_id))
