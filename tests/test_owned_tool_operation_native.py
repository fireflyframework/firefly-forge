# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Owned PostgreSQL broker acceptance with fictional callbacks and no provider calls."""

import asyncio
import copy

import pytest

from firefly_forge import tool_broker as module
from firefly_forge.agent_roles import ProjectRoles
from firefly_forge.journal import Journal
from firefly_forge.models import RepositoryInput, RunInput
from firefly_forge.sandbox_models import SandboxInput, capture_sandbox
from firefly_forge.work_duration_models import WorkDurationPolicyV2
from firefly_forge.workspace_tools import tool_catalog

SOURCE = {"base_revision": "a" * 40, "head": "a" * 40, "source_tree": "b" * 40}


async def seed(store, tmp_path, monkeypatch, *, chosen=None):
    await store.acquire_ownership()
    monkeypatch.setattr(module, "OWNERSHIP_GRACE_SECONDS", 1)
    broker = module.ToolInvocationBroker(store, Journal(store), tmp_path / "broker")
    broker.configure(module.ToolPolicyInput(timeout_seconds=1))
    repo = await store.add_repository(
        RepositoryInput(name="Fictional owned call", url="https://example.invalid/owned.git")
    )
    await store.set_repository_roles(repo["id"], ProjectRoles(allowed_tools={"implementer": ["internet_search"]}))
    duration = WorkDurationPolicyV2(timeout_seconds=chosen)
    run = await store.create_run(
        RunInput(repository_id=repo["id"], brief="Fictional controlled operation", agent="agentic"),
        prepared_runner={
            "sandbox_snapshot": capture_sandbox(SandboxInput(internet_search=True, work_duration=duration)),
            "work_duration": duration.model_dump(mode="json"),
            "search_snapshot": {"revision": "a" * 32},
            "tool_policy_snapshot": broker.snapshot(),
            "runner_config_revision": None,
        },
        prepared_tools=tool_catalog({"internet_search": True}),
    )
    await broker.journal.intent(run["id"], "implement")
    await store.update(run["id"], status="developing", attempt=1)
    return broker, await store.get_run(run["id"])


async def invoke(broker, run, callback):
    return await broker.invoke(
        run["id"],
        "implement",
        "internet_search",
        "1" * 32,
        {"query": "approved source", "max_results": 1},
        callback,
        source=SOURCE,
        configuration=run["search_snapshot"],
    )


async def test_native_nullable_operation_renews_durable_grace_and_two_service_reconcile_cannot_abandon_live_call(
    store, store_factory, tmp_path, monkeypatch
):
    broker, run = await seed(store, tmp_path, monkeypatch)
    api_store = await store_factory(store.path)
    api = module.ToolInvocationBroker(api_store, Journal(api_store), tmp_path / "broker")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def work():
        calls.append(1)
        entered.set()
        await release.wait()
        return {"results": []}

    task = asyncio.create_task(invoke(broker, run, work))
    await entered.wait()
    await asyncio.sleep(1.15)
    try:
        before = await api.invocations(run["id"])
        assert before[0]["ownership"]["sequence"] >= 2
        with pytest.raises(ValueError, match="still be running"):
            await api.reconcile(run["id"], "1" * 32, "2" * 32)
        assert (await api.invocations(run["id"]))[0]["status"] == "pending"
    finally:
        release.set()
    result = await task
    assert await invoke(api, run, work) == result and calls == [1]
    saved = (await api.invocations(run["id"]))[0]
    assert saved["status"] == "succeeded" and saved["admission"]["operation"]["work_deadline_ms"] is None
    assert Journal.verify_chain(await broker.journal.entries(run["id"]))
    assert [x["kind"] for x in await broker.journal.entries(run["id"])][-2:] == ["tool_intent", "tool_receipt"]


async def test_native_chosen_work_deadline_cancels_once_and_never_replays_unknown(store, tmp_path, monkeypatch):
    broker, run = await seed(store, tmp_path, monkeypatch, chosen=1)
    calls = []

    async def work():
        calls.append("entered")
        try:
            await asyncio.Event().wait()
        finally:
            calls.append("drained")

    with pytest.raises(module.ToolReconciliationRequired):
        await invoke(broker, run, work)
    row = (await broker.invocations(run["id"]))[0]
    assert row["status"] == "unknown" and row["error"]["operation_outcome"] == "timed_out"
    assert (
        row["admission"]["operation"]["work_deadline_ms"] == row["admission"]["operation"]["work_started_at_ms"] + 1000
    )
    with pytest.raises(module.ToolReconciliationRequired):
        await invoke(broker, run, work)
    assert calls == ["entered", "drained"]


async def test_native_late_known_output_after_factory_owner_loss_is_unavailable(store, tmp_path, monkeypatch):
    broker, run = await seed(store, tmp_path, monkeypatch)

    async def work():
        await store.release_ownership()
        return {"results": [{"late": True}]}

    with pytest.raises(module.ToolReconciliationRequired):
        await invoke(broker, run, work)
    assert (await broker.invocations(run["id"]))[0]["status"] != "succeeded"
    assert not any(
        x["kind"] == "tool_receipt" and x["payload"]["status"] == "succeeded"
        for x in await broker.journal.entries(run["id"])
    )


async def test_native_post_receipt_authority_fault_rolls_back_known_result_before_unknown_record(
    store, tmp_path, monkeypatch
):
    broker, run = await seed(store, tmp_path, monkeypatch)
    original = broker.journal._append
    fault = []

    async def append(*args):
        event = await original(*args)
        if args[3] == "tool_receipt":
            fault.append(1)
            changed = copy.deepcopy(run["work_duration"])
            changed["timeout_seconds"] = 1
            await store.update(run["id"], work_duration=changed)
        return event

    monkeypatch.setattr(broker.journal, "_append", append)

    async def work():
        return {"results": []}

    with pytest.raises(module.ToolReconciliationRequired):
        await invoke(broker, run, work)
    assert fault == [1]
    assert (await store.get_run(run["id"]))["work_duration"] == run["work_duration"]
    rows = await broker.invocations(run["id"])
    assert len(rows) == 1 and rows[0]["status"] == "unknown"
    history = await broker.journal.entries(run["id"])
    assert Journal.verify_chain(history) and not any(x["kind"] == "tool_receipt" for x in history)
