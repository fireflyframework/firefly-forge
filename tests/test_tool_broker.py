# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import asyncio
import importlib
import json
import time

import pytest

from firefly_forge.agent_roles import ProjectRoles
from firefly_forge.journal import Journal
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
from firefly_forge.workspace_tools import tool_catalog


async def seed(store, tmp_path, *, limit=3, max_bytes=256_000):
    module = importlib.import_module("firefly_forge.tool_broker")
    broker = module.ToolInvocationBroker(store, Journal(store), tmp_path / "broker")
    broker.configure(module.ToolPolicyInput(calls_limit=limit, per_tool_limit=limit, max_result_bytes=max_bytes))
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


@pytest.mark.asyncio
async def test_two_instances_use_database_lease_time_despite_host_clock_skew(
    store, store_factory, tmp_path, monkeypatch
):
    module, worker, run, source = await seed(store, tmp_path)
    api_store = await store_factory(store.path)
    api = module.ToolInvocationBroker(api_store, Journal(api_store), tmp_path / "broker")
    started, release = asyncio.Event(), asyncio.Event()

    async def callback():
        started.set()
        await release.wait()
        return {"results": []}

    monkeypatch.setattr(time, "time", lambda: 0)
    pending = asyncio.create_task(invoke(worker, run, source, "1" * 32, callback))
    await asyncio.wait_for(started.wait(), 3)
    monkeypatch.setattr(time, "time", lambda: 10_000_000_000)
    try:
        with pytest.raises(ValueError, match="running"):
            await api.reconcile(run["id"], "1" * 32, "2" * 32)
    finally:
        release.set()
        outcomes = await asyncio.gather(pending, return_exceptions=True)
    assert outcomes == [{"results": [], "invocation_id": "1" * 32}]
    assert (await api.invocations(run["id"]))[0]["status"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("final_size", [1024, 1025])
async def test_final_packet_size_includes_correlation_metadata(store, tmp_path, final_size):
    module, broker, run, source = await seed(store, tmp_path, max_bytes=1024)
    overhead = len(json.dumps({"value": "", "invocation_id": "1" * 32}).encode())
    payload = {"value": "x" * (final_size - overhead)}

    async def callback():
        return payload

    if final_size == 1024:
        result = await invoke(broker, run, source, "1" * 32, callback)
        assert len(json.dumps(result).encode()) == 1024
        assert (await broker.invocations(run["id"]))[0]["result"] == result
    else:
        with pytest.raises(module.ToolReconciliationRequired):
            await invoke(broker, run, source, "1" * 32, callback)
        assert (await broker.invocations(run["id"]))[0]["status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,status", [("quota", 432), ("rate_limit", 429), ("authentication", 401)])
async def test_rejected_receipt_replay_preserves_typed_error_without_another_request(store, tmp_path, kind, status):
    from firefly_forge.search_tools import SearchProviderError

    _, broker, run, source = await seed(store, tmp_path)
    calls = []

    async def callback():
        calls.append(1)
        raise SearchProviderError("tavily", kind, "Safe provider rejection", status=status)

    for _ in range(2):
        with pytest.raises(SearchProviderError) as raised:
            await invoke(broker, run, source, "1" * 32, callback)
        assert raised.value.kind == kind and raised.value.status == status
    assert calls == [1]
