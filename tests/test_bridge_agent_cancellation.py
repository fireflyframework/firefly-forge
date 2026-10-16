# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real Agentic taskgraph controls with an in-process SDK transport, no database."""

import asyncio

import httpx2
import pytest
from test_bridge_inflight_cancel import RUN, MemoryStore, Rows

from firefly_forge import engine as engine_module
from firefly_forge.controllers import RunService
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.providers import BridgeProvider, ProviderSubmissionCancelled


@pytest.mark.parametrize(
    "boundary", ["before-post", "pending-post", "completed-then-before-post", "completed-then-pending-post"]
)
async def test_actual_agent_engine_cancel_preserves_only_unresolved_submission(tmp_path, monkeypatch, boundary):
    store = MemoryStore(tmp_path / "not-a-database")
    journal = Journal(store, plans=Rows(), tasks=Rows(), records=Rows())
    provider = BridgeProvider({"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"})
    model = provider.model()
    await model.client._client.aclose()
    barrier = asyncio.Event()
    calls = []
    mapped = 0
    original_map = model._map_messages

    async def map_messages(*args, **kwargs):
        nonlocal mapped
        mapped += 1
        if boundary == "before-post" or (boundary == "completed-then-before-post" and mapped == 2):
            barrier.set()
            await asyncio.Event().wait()
        return await original_map(*args, **kwargs)

    async def respond(request):
        calls.append(request)
        if boundary == "pending-post" or (boundary == "completed-then-pending-post" and len(calls) == 2):
            barrier.set()
            await asyncio.Event().wait()
        return httpx2.Response(
            200,
            json={
                "id": "fictional",
                "object": "chat.completion",
                "model": "bridge/default",
                "created": 1,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "Please produce a valid structured report next.",
                        },
                    }
                ],
            },
        )

    model.client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    monkeypatch.setattr(model, "_map_messages", map_messages)
    monkeypatch.setattr(provider, "model", lambda **kwargs: model)
    engine = Engine(store, tmp_path / "workspaces", lambda _: provider, journal=journal)
    workspace = engine.workspaces / RUN
    workspace.mkdir()
    (workspace / "partial.txt").write_text("Retained original partial work\n")

    async def inspect(*args):
        return {"base_revision": "b" * 40, "head": "b" * 40, "source_tree": "c" * 40}

    async def checked(argv, *args, **kwargs):
        assert argv == ["git", "ls-files", "-z"]
        return ""

    monkeypatch.setattr(engine, "_inspect_workspace", inspect)
    monkeypatch.setattr(engine_module, "checked", checked)
    execution = asyncio.create_task(engine.execute_run(RUN))
    engine.active[RUN] = execution
    try:
        waiting = asyncio.create_task(barrier.wait())
        try:
            await asyncio.wait({waiting, execution}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
            assert barrier.is_set(), (store.run.data, store.events, execution.exception() if execution.done() else None)
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        await engine.cancel_effect(RUN)
        assert execution.done() and execution.cancelled()
        run = await store.get_run(RUN)
        assert run["status"] == "cancelled"
        assert len(calls) == (0 if boundary == "before-post" else 2 if boundary == "completed-then-pending-post" else 1)
        implementation = next(row for row in journal.tasks.rows if row.task_id == "implement")
        entries = [row.data for row in journal.records.rows]
        assert Journal.verify_chain(entries)
        assert (workspace / "partial.txt").read_text() == "Retained original partial work\n"
        if boundary in {"pending-post", "completed-then-pending-post"}:
            assert run["pause"]["kind"] == "provider_outcome_unknown"
            assert run["pause"]["automatic"] is False and run["pause"]["retry_at"] is None
            assert implementation.status == "needs_reconciliation"
            errors = [row for row in entries if row["kind"] == "error" and row["task_id"] == "implement"]
            assert len(errors) == 1 and errors[0]["idempotency_key"] == implementation.data["idempotency_key"]
            assert "provider_observation" not in errors[0]["payload"]
            with pytest.raises(ValueError, match="Bridge.*reconcil"):
                await RunService(store, engine).retry(RUN)
            assert store.created == []
        else:
            assert "pause" not in run and implementation.status == "running"
            assert (await RunService(store, engine).retry(RUN))["status"] == "queued"
            assert len(store.created) == 1
    finally:
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        await model.client.close()


async def test_actual_agent_concurrent_invocations_do_not_share_unresolved_state(tmp_path, monkeypatch):
    provider = BridgeProvider({"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"})
    model = provider.model()
    await model.client._client.aclose()
    pending, before = asyncio.Event(), asyncio.Event()
    calls = []
    original_map = model._map_messages

    async def map_messages(messages, *args, **kwargs):
        if "known cancellation" in repr(messages):
            before.set()
            await asyncio.Event().wait()
        return await original_map(messages, *args, **kwargs)

    async def respond(request):
        calls.append(request)
        pending.set()
        await asyncio.Event().wait()

    async def emit(*args):
        pass

    model.client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    monkeypatch.setattr(model, "_map_messages", map_messages)
    monkeypatch.setattr(provider, "model", lambda **kwargs: model)
    tasks = []

    async def cancel_known():
        workspace = tmp_path / str(len(tasks))
        workspace.mkdir()
        before.clear()
        task = asyncio.create_task(provider.execute(workspace, "known cancellation", "pyfly", "none", emit))
        tasks.append(task)
        await asyncio.wait_for(before.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert not isinstance(caught.value, ProviderSubmissionCancelled)
        assert task.cancelled()

    try:
        workspace = tmp_path / "pending"
        workspace.mkdir()
        unknown = asyncio.create_task(provider.execute(workspace, "unresolved submission", "pyfly", "none", emit))
        tasks.append(unknown)
        await asyncio.wait_for(pending.wait(), 5)
        await cancel_known()
        unknown.cancel()
        with pytest.raises(ProviderSubmissionCancelled):
            await unknown
        assert unknown.cancelled() and len(calls) == 1
        await cancel_known()
        assert len(calls) == 1
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await model.client.close()


@pytest.mark.parametrize("stream", [False, True])
async def test_one_completed_request_cannot_clear_another_pending_request(tmp_path, monkeypatch, stream):
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai.models import ModelRequestParameters

    from firefly_forge import agentic_runtime

    provider = BridgeProvider({"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"})
    model = provider.model()
    await model.client._client.aclose()
    pending, complete = asyncio.Event(), asyncio.Event()
    calls = []

    class HeldStream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield (
                b'data: {"id":"fixture","model":"bridge/default","created":1,'
                b'"choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
            )
            pending.set()
            await asyncio.Event().wait()

    async def respond(request):
        calls.append(request)
        if b"pending" in request.content:
            if stream:
                return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=HeldStream())
            pending.set()
            await asyncio.Event().wait()
        return httpx2.Response(
            200,
            json={
                "id": "fictional",
                "object": "chat.completion",
                "model": "bridge/default",
                "created": 1,
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Known result"}}
                ],
            },
        )

    async def infer(text):
        messages = [ModelRequest(parts=[UserPromptPart(text)])]
        if stream and text == "pending":
            async with model.request_stream(messages, None, ModelRequestParameters()) as response:
                async for _ in response:
                    pass
        else:
            await model.request(messages, None, ModelRequestParameters())

    async def two_requests(*args, **kwargs):
        # This controlled scheduling seam tests the invocation boundary, not the Agentic graph.
        task = asyncio.create_task(infer("pending"))
        try:
            await pending.wait()
            await infer("complete")
            complete.set()
            await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def emit(*args):
        pass

    model.client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    monkeypatch.setattr(provider, "model", lambda **kwargs: model)
    monkeypatch.setattr(agentic_runtime, "execute_agentic", two_requests)
    task = asyncio.create_task(provider.execute(tmp_path, "Offline scope control", "pyfly", "none", emit))
    try:
        await asyncio.wait_for(complete.wait(), 5)
        task.cancel()
        with pytest.raises(ProviderSubmissionCancelled):
            await task
        assert task.cancelled() and len(calls) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await model.client.close()


@pytest.mark.parametrize(
    "status,code,kind", [(429, "quota_exhausted", "quota"), (503, "execution_paused", "provider_paused")]
)
async def test_actual_agent_keeps_known_manual_pause_classification(tmp_path, monkeypatch, status, code, kind):
    from firefly_forge.providers import ProviderLimitError

    provider = BridgeProvider({"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"})
    model = provider.model()
    await model.client._client.aclose()
    calls = []

    def respond(request):
        calls.append(request)
        return httpx2.Response(status, json={"error": {"code": code, "message": "Opaque upstream details"}})

    async def emit(*args):
        pass

    model.client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    monkeypatch.setattr(provider, "model", lambda **kwargs: model)
    try:
        with pytest.raises(ProviderLimitError) as caught:
            await provider.execute(tmp_path, "Offline known pause", "pyfly", "none", emit)
        assert caught.value.kind == kind
        assert len(calls) == 1 and model.client.max_retries == 0
    finally:
        await model.client.close()
