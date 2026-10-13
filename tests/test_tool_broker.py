# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio
import importlib

import pytest

from firefly_forge.agent_roles import ProjectRoles
from firefly_forge.journal import Journal
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
from firefly_forge.workspace_tools import tool_catalog


async def seed(store, tmp_path, *, limit=3):
    module = importlib.import_module("firefly_forge.tool_broker")
    broker = module.ToolInvocationBroker(store, Journal(store), tmp_path / "broker")
    broker.configure(module.ToolPolicyInput(calls_limit=limit, per_tool_limit=limit))
    repo = await store.add_repository(RepositoryInput(name="product", url="https://github.com/team/product.git"))
    await store.set_repository_roles(repo["id"], ProjectRoles(allowed_tools={"implementer": ["internet_search"]}))
    snapshot = {
        "provider": "tavily",
        "endpoint": "https://api.tavily.com/search",
        "revision": "a" * 32,
        "configured": True,
        "max_results": 10,
        "live_search_verified": False,
        "include_domains": ["github.com"],
    }
    run = await store.create_run(
        RunInput(repository_id=repo["id"], brief="Implement stock alerts", agent="agentic"),
        prepared_runner={
            "sandbox_snapshot": capture_sandbox(SandboxInput(internet_search=True)),
            "search_snapshot": snapshot,
            "tool_policy_snapshot": broker.snapshot(),
            "runner_config_revision": None,
        },
        prepared_tools=tool_catalog({"internet_search": True}),
    )
    await broker.journal.intent(run["id"], "implement")
    await store.update(run["id"], status="generating")
    assert run["tool_policy_snapshot"]["settings"]["calls_limit"] == limit
    source = {"base_revision": "a" * 40, "head": "a" * 40, "source_tree": "b" * 40}
    return module, broker, run, source


async def invoke(broker, run, source, correlation, callback, **kwargs):
    return await broker.invoke(
        run["id"],
        "implement",
        "internet_search",
        correlation,
        {"query": "pyfly DI", "max_results": 1},
        callback,
        source=source,
        configuration=run["search_snapshot"],
        **kwargs,
    )


@pytest.mark.asyncio
async def test_invocation_intent_precedes_callback_and_duplicate_returns_same_receipt(store, tmp_path):
    _, broker, run, source = await seed(store, tmp_path)
    calls = []

    async def callback():
        calls.append(1)
        entries = await broker.journal.entries(run["id"])
        assert entries[-1]["kind"] == "tool_intent"
        assert entries[-1]["payload"]["correlation_id"] == "1" * 32
        assert entries[-1]["payload"]["source"] == source
        return {"results": [{"citation_id": "web-" + "a" * 24, "url": "https://github.com/fireflyframework"}]}

    first = await invoke(broker, run, source, "1" * 32, callback)
    assert await invoke(broker, run, source, "1" * 32, callback) == first
    assert len(calls) == 1
    events = await broker.journal.entries(run["id"])
    assert Journal.verify_chain(events)
    assert [event["kind"] for event in events][-2:] == ["tool_intent", "tool_receipt"]
    assert (await broker.invocations(run["id"]))[0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_unknown_invocation_blocks_all_new_calls_until_explicit_abandonment(store, tmp_path):
    module, broker, run, source = await seed(store, tmp_path)
    calls = []

    async def timeout():
        calls.append(1)
        raise TimeoutError("untrusted provider details")

    with pytest.raises(module.ToolReconciliationRequired):
        await invoke(broker, run, source, "1" * 32, timeout)
    assert (await broker.invocations(run["id"]))[0]["status"] == "unknown"
    with pytest.raises(module.ToolReconciliationRequired):
        await invoke(broker, run, source, "2" * 32, timeout)
    await broker.reconcile(run["id"], "1" * 32, "3" * 32)
    with pytest.raises(ValueError, match="abandoned"):
        await invoke(broker, run, source, "1" * 32, timeout)
    assert calls == [1]
    events = await broker.journal.entries(run["id"])
    assert Journal.verify_chain(events) and events[-1]["kind"] == "tool_reconciled"


@pytest.mark.asyncio
async def test_budget_is_reserved_atomically_across_concurrent_invocations(store, tmp_path):
    _, broker, run, source = await seed(store, tmp_path, limit=1)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def callback():
        calls.append(1)
        started.set()
        await release.wait()
        return {"results": []}

    first = asyncio.create_task(invoke(broker, run, source, "1" * 32, callback))
    await asyncio.wait_for(started.wait(), 3)
    try:
        with pytest.raises(ValueError, match="budget"):
            await invoke(broker, run, source, "2" * 32, callback)
    finally:
        release.set()
        await first
    assert calls == [1] and len(await broker.invocations(run["id"])) == 1


@pytest.mark.asyncio
async def test_cancellation_retains_unknown_intent_and_forbids_reconciliation_of_live_callback(store, tmp_path):
    _, broker, run, source = await seed(store, tmp_path)
    started = asyncio.Event()

    async def callback():
        started.set()
        await asyncio.Event().wait()
        return {}

    pending = asyncio.create_task(invoke(broker, run, source, "1" * 32, callback))
    await asyncio.wait_for(started.wait(), 3)
    with pytest.raises(ValueError, match="running"):
        await broker.reconcile(run["id"], "1" * 32, "2" * 32)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert (await broker.invocations(run["id"]))[0]["status"] == "unknown"
    assert Journal.verify_chain(await broker.journal.entries(run["id"]))


@pytest.mark.asyncio
async def test_queued_policy_remains_frozen_and_database_rejects_admission_tampering(store, tmp_path):
    from sqlalchemy import update
    from sqlalchemy.exc import DBAPIError

    from firefly_forge.tool_persistence import ForgeToolInvocationEntity

    module, broker, run, source = await seed(store, tmp_path, limit=1)
    broker.configure(module.ToolPolicyInput(calls_limit=30))

    async def callback():
        return {"results": []}

    await invoke(broker, run, source, "1" * 32, callback)
    with pytest.raises(ValueError, match="budget"):
        await invoke(broker, run, source, "2" * 32, callback)
    with pytest.raises(DBAPIError, match="immutable"):
        async with store._unit() as session:
            await session.execute(update(ForgeToolInvocationEntity).values(tool="wrong-tool"))


@pytest.mark.asyncio
async def test_invocation_correlation_binds_arguments_and_frozen_configuration(store, tmp_path):
    _, broker, run, source = await seed(store, tmp_path)

    async def callback():
        return {"results": []}

    await invoke(broker, run, source, "1" * 32, callback)
    with pytest.raises(ValueError, match="correlation"):
        await broker.invoke(
            run["id"],
            "implement",
            "internet_search",
            "1" * 32,
            {"query": "different"},
            callback,
            source=source,
            configuration=run["search_snapshot"],
        )
    with pytest.raises(ValueError, match="frozen"):
        await broker.invoke(
            run["id"],
            "implement",
            "internet_search",
            "2" * 32,
            {"query": "pyfly"},
            callback,
            source=source,
            configuration={**run["search_snapshot"], "include_domains": []},
        )


@pytest.mark.asyncio
async def test_known_credentials_never_enter_invocation_wal(store, tmp_path):
    _, broker, run, source = await seed(store, tmp_path)
    broker.credential_registry = lambda: {"MCP_API_KEY": "never-persist-this-key"}

    async def callback():
        return {"snippet": "never-persist-this-key"}

    with pytest.raises(ValueError, match="credential"):
        await broker.invoke(
            run["id"],
            "implement",
            "internet_search",
            "1" * 32,
            {"query": "never-persist-this-key"},
            callback,
            source=source,
            configuration=run["search_snapshot"],
        )
    assert await broker.invocations(run["id"]) == []
    with pytest.raises(RuntimeError):
        await invoke(broker, run, source, "2" * 32, callback)
    assert "never-persist-this-key" not in str(await broker.journal.entries(run["id"]))
