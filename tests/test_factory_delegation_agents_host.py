# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Independent public Agentic runs spend one immutable shared family envelope."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from test_factory_delegation_dispatch_host import prepared


async def test_public_agentic_child_uses_owned_model_scope_and_shared_multiturn_budget(tmp_path, monkeypatch):
    manager, session, native, child, _ = await prepared(tmp_path, monkeypatch)
    import firefly_forge.delegation_agent_budget as module

    assert hasattr(module, "FamilyAgentCapability"), "Missing public Agentic shared-budget capability"
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "app.py").write_text("value = 1\n")
    opened, closed, calls = [], [], []

    async def respond(messages, info):
        calls.append(messages)
        if len(calls) == 1:
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "app.py"}, "read-original")])
        return ModelResponse(
            parts=[TextPart(json.dumps({"summary": "Observed value = 1", "changes": [], "delivery_verified": False}))]
        )

    @asynccontextmanager
    async def models(run):
        opened.append(copy.deepcopy(run["model_selection"]))
        try:
            yield FunctionModel(respond)
        finally:
            closed.append(run["id"])

    from fireflyframework_agentic.agents import FireflyAgent
    from fireflyframework_agentic.config import FireflyAgenticConfig
    from pydantic_ai import PromptedOutput
    from pydantic_ai.usage import UsageLimits

    from firefly_forge.delegation_lifecycle import ChildResult
    from firefly_forge.workspace_tools import WorkspaceTools

    async def worker(run):
        capability = module.FamilyAgentCapability(manager, session, run["id"])
        async with models(run) as model:
            agent = FireflyAgent(
                "controlled-factory-child",
                model=model,
                output_type=PromptedOutput(ChildResult),
                tools=[WorkspaceTools(origin).read_file],
                capabilities=[capability],
                auto_register=False,
                retries=0,
                config=FireflyAgenticConfig(default_temperature=None, rate_limit_max_retries=0),
            )
            try:
                result = await agent.run(
                    run["brief"], timeout=None, usage_limits=UsageLimits(request_limit=None, tool_calls_limit=None)
                )
                return result.output.model_dump(mode="json")
            finally:
                await capability.close()

    manager.runtime.worker = worker
    result = await manager.runtime.execute_child(child["id"])
    assert result["state"] == "completed" and result["summary"] == "Observed value = 1"
    assert opened == [child["model_selection"]] and closed == [child["id"]]
    state = await manager.ledger.state(session.run_id)
    assert state["charged"]["requests"] == state["attempted"]["requests"] == 2
    assert state["charged"]["tool_calls"] == state["attempted"]["tool_calls"] == 1
    assert state["unknown"]["requests"] == state["unknown"]["tool_calls"] == 0
    assert len(calls) == 2 and (origin / "app.py").read_text() == "value = 1\n"
    assert not any(item.get("applied") for item in [result])


async def pair(tmp_path, monkeypatch):
    manager, session, native, first, _ = await prepared(tmp_path, monkeypatch)
    from firefly_forge.delegation_contract import child_run
    from firefly_forge.delegation_models import DelegationStartInput
    from firefly_forge.factory_delegations import approval_digest
    from firefly_forge.journal import digest

    request = DelegationStartInput(request_id="e" * 32, task_id="second", brief="Independent second analysis")
    second = child_run(native.run, request, first["factory_context"]["source"])
    second["factory_context"]["parent_task_key"] = session.task_key
    second["factory_context"]["digest"] = digest({k: v for k, v in second["factory_context"].items() if k != "digest"})
    async with native.store._unit():
        await manager.ledger.append_locked(
            session.run_id,
            "child_admitted",
            "second-child",
            {
                "parent_run_id": session.run_id,
                "parent_task_key": session.task_key,
                "child_run_id": second["id"],
                "request": request.model_dump(mode="json"),
                "approval_digest": approval_digest(second),
                "source_digest": second["factory_context"]["source"]["digest"],
            },
        )
    original = native.store.get_run

    async def get_run(identity):
        return copy.deepcopy(second) if identity == second["id"] else await original(identity)

    native.store.get_run = get_run
    return manager, session, native, [first, second]


async def test_engine_runs_parallel_independent_public_agents_then_reacquires_parent_before_join(tmp_path, monkeypatch):
    manager, session, native, children = await pair(tmp_path, monkeypatch)
    from fireflyframework_agentic.agents import FireflyAgent
    from fireflyframework_agentic.config import FireflyAgenticConfig
    from pydantic_ai import PromptedOutput
    from pydantic_ai.usage import UsageLimits

    from firefly_forge.delegation_agent_budget import FamilyAgentCapability
    from firefly_forge.delegation_lifecycle import ChildResult
    from firefly_forge.delegation_models import DelegationJoinInput, DelegationWaitInput
    from firefly_forge.engine import Engine

    entered, closed, models = set(), set(), []
    both, release = asyncio.Event(), asyncio.Event()

    async def worker(run):
        capability = FamilyAgentCapability(manager, session, run["id"])

        async def respond(messages, info):
            entered.add(run["id"])
            if len(entered) == 2:
                both.set()
            await release.wait()
            return ModelResponse(
                parts=[TextPart(json.dumps({"summary": run["brief"], "changes": [], "delivery_verified": False}))]
            )

        model = FunctionModel(respond, model_name=run["id"])
        models.append(model)
        agent = FireflyAgent(
            "controlled-parallel-child",
            model=model,
            output_type=PromptedOutput(ChildResult),
            capabilities=[capability],
            auto_register=False,
            retries=0,
            config=FireflyAgenticConfig(default_temperature=None, rate_limit_max_retries=0),
        )
        try:
            result = await agent.run(
                run["brief"], timeout=None, usage_limits=UsageLimits(request_limit=None, tool_calls_limit=None)
            )
            return result.output.model_dump(mode="json")
        finally:
            await capability.close()
            closed.add(run["id"])

    async def queued():
        return []

    native.store.path, native.store.runtime_roles = tmp_path / "factory.db", None
    native.store.queued = queued
    manager.runtime.worker = worker
    engine = Engine(native.store, tmp_path / "workspaces", concurrency=2)
    engine.factory_delegations = manager
    waiting = asyncio.create_task(
        session.wait(DelegationWaitInput(request_id="6" * 32, delegation_ids=[child["id"] for child in children]))
    )
    engine.active[session.run_id] = waiting
    engine.active_projects[session.run_id] = native.run["repository_id"]
    try:
        await manager.runtime.changed.wait()
        await engine.dispatch_queued()
        await asyncio.wait_for(both.wait(), 2)
        assert len(models) == 2 and models[0] is not models[1]
        assert len(engine.active) == 3 and not waiting.done()
        release.set()
        await asyncio.gather(*(task for identity, task in engine.active.items() if identity != session.run_id))
        while next(iter(manager.runtime.waits.values())).stage != "reacquiring":
            await asyncio.sleep(0)
        assert not waiting.done()
        await engine.dispatch_queued()
        outcome = await asyncio.wait_for(waiting, 2)
        assert outcome["outcome"] == "satisfied" and outcome["resumed"]
        joined = await session.join(
            DelegationJoinInput(request_id="7" * 32, delegation_ids=[child["id"] for child in reversed(children)])
        )
        assert joined["status"] == "ready" and not joined["applied"]
        assert [row["task_id"] for row in joined["children"]] == ["analysis", "second"]
        state = await manager.ledger.state(session.run_id)
        assert state["charged"]["requests"] == state["attempted"]["requests"] == 2
        assert closed == entered == {child["id"] for child in children}
    finally:
        release.set()
        for task in engine.active.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*engine.active.values(), return_exceptions=True)
