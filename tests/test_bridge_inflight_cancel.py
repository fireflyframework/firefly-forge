# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Offline SDK cancellation in the actual Engine implementation stage; no sockets."""

import asyncio
from contextlib import asynccontextmanager
from types import MethodType, SimpleNamespace
from uuid import UUID

import httpx2
import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from firefly_forge import engine as engine_module
from firefly_forge.controllers import RunService
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.models import RunInput
from firefly_forge.providers import BridgeProvider
from firefly_forge.store import Store

RUN = UUID(int=1).hex
REPOSITORY = UUID(int=2).hex


class Rows:
    def __init__(self, rows=None):
        self.rows = [] if rows is None else rows

    async def find_by_id(self, identifier, **kwargs):
        return next((row for row in self.rows if row.id == identifier), None)

    async def find_for_run(self, identifier):
        return self.rows

    async def find_current(self, identifier):
        return self.rows[0] if self.rows else None

    async def save(self, row):
        if all(existing is not row for existing in self.rows):
            self.rows.append(row)


class MemoryStore:
    runtime_roles = None

    def __init__(self, path):
        self.path = path
        request = RunInput(
            repository_id=REPOSITORY, brief="Offline cancellation boundary", agent="bridge", frontend="none", skills=[]
        )
        self.run = SimpleNamespace(
            id=UUID(RUN),
            status="running",
            data={
                **request.model_dump(mode="json"),
                "status": "running",
                "repair_budget": 0,
                "resume_existing": True,
                "git_metadata_digest": "a" * 64,
            },
        )
        self.run_records = Rows([self.run])
        self.events = []
        self.created = []
        self.finish = MethodType(Store.finish, self)

    @asynccontextmanager
    async def _unit(self):
        yield

    async def get_run(self, identifier):
        return {"id": identifier, **self.run.data}

    async def claim(self, identifier):
        return True

    async def update(self, identifier, **values):
        self.run.data.update(values)
        self.run.status = self.run.data["status"]

    async def event(self, *values):
        self.events.append(values)

    async def repository(self, identifier):
        return {"id": identifier, "url": "fictional-source-only"}

    async def admit_credentials(self, value):
        return None

    async def create_run(self, request):
        self.created.append(request)
        return {"id": UUID(int=3).hex, "status": "queued"}


@pytest.mark.parametrize("boundary", ["before-dispatch", "after-dispatch", "stream-wait"])
@pytest.mark.parametrize("stopping", [False, True])
async def test_bridge_inference_cancellation_retains_only_dispatched_unknown(tmp_path, monkeypatch, boundary, stopping):
    store = MemoryStore(tmp_path / "not-a-database.db")
    journal = Journal(store, plans=Rows(), tasks=Rows(), records=Rows())
    model = BridgeProvider({"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}).model()
    await model.client._client.aclose()
    dispatched = asyncio.Event()
    blocked = asyncio.Event()
    calls = []
    cancelled_at_transport = []

    class Chunks(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield (
                b'data: {"id":"fixture","model":"bridge/default","created":1,'
                b'"choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
            )
            dispatched.set()
            try:
                await blocked.wait()
            except asyncio.CancelledError:
                cancelled_at_transport.append(True)
                raise

    async def respond(request):
        calls.append(request)
        if boundary == "stream-wait":
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks())
        dispatched.set()
        try:
            await blocked.wait()
        except asyncio.CancelledError:
            cancelled_at_transport.append(True)
            raise
        raise AssertionError("No response is delivered in this cancellation control")

    model.client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    if boundary == "before-dispatch":

        async def before_dispatch(*args, **kwargs):
            dispatched.set()
            await blocked.wait()

        monkeypatch.setattr(model, "_map_messages", before_dispatch)

    class ActualSDKProvider:
        async def execute(self, workspace, brief, backend, frontend, emit):
            messages = [ModelRequest(parts=[UserPromptPart(brief)])]
            if boundary == "stream-wait":
                async with model.request_stream(messages, None, ModelRequestParameters()) as response:
                    async for _ in response:
                        pass
            else:
                return await model.request(messages, None, ModelRequestParameters())

    engine = Engine(store, tmp_path / "workspaces", lambda _: ActualSDKProvider(), journal=journal)
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
    engine.stopping = stopping
    try:
        await asyncio.wait_for(dispatched.wait(), 5)
        before = await journal.plan(RUN)
        assert next(task for task in before["tasks"] if task["id"] == "implement")["status"] == "running"
        result = await engine.cancel_effect(RUN)
        assert execution.done() and execution.cancelled()
        assert result == {"status": "cancelled", "worker_drained": True}
        after = await store.get_run(RUN)
        assert after["status"] == ("interrupted" if stopping else "cancelled")
        plan = await journal.plan(RUN)
        implementation = next(task for task in plan["tasks"] if task["id"] == "implement")
        entries = [row.data for row in journal.records.rows]
        assert Journal.verify_chain(entries)
        assert (workspace / "partial.txt").read_text() == "Retained original partial work\n"
        if boundary == "before-dispatch":
            assert calls == [] and cancelled_at_transport == []
            assert "pause" not in after
            assert implementation["status"] == "running"
            assert (await RunService(store, engine).retry(RUN))["status"] == "queued"
            assert len(store.created) == 1
        else:
            assert len(calls) == 1 and cancelled_at_transport == [True]
            assert after["pause"]["kind"] == "provider_outcome_unknown"
            assert after["pause"]["automatic"] is False and after["pause"]["retry_at"] is None
            assert implementation["status"] == "needs_reconciliation"
            intent = next(entry for entry in entries if entry["kind"] == "intent" and entry["task_id"] == "implement")
            error = next(entry for entry in entries if entry["kind"] == "error" and entry["task_id"] == "implement")
            assert intent["idempotency_key"] == error["idempotency_key"] == implementation["idempotency_key"]
            assert "provider_observation" not in error["payload"]
            with pytest.raises(ValueError, match="Bridge.*reconcil"):
                await RunService(store, engine).retry(RUN)
            assert store.created == []
    finally:
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        await model.client.close()


@pytest.mark.parametrize("complete", [False, True])
async def test_cancelled_stream_consumer_is_unknown_only_until_stream_completion(complete):
    from firefly_forge import providers

    model = BridgeProvider({"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}).model()
    await model.client._client.aclose()
    calls = []

    def respond(request):
        calls.append(request)
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text='data: {"id":"fixture","model":"bridge/default","created":1,'
            '"choices":[{"index":0,"delta":{"content":"complete"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
        )

    model.client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    cancellation = asyncio.CancelledError("Local cancellation")
    try:
        with pytest.raises(asyncio.CancelledError) as caught:
            async with model.request_stream(
                [ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters()
            ) as response:
                if complete:
                    async for _ in response:
                        pass
                raise cancellation
        assert len(calls) == 1
        if complete:
            assert caught.value is cancellation
        else:
            assert isinstance(caught.value, getattr(providers, "ProviderSubmissionCancelled", ()))
            assert caught.value.__cause__ is cancellation
    finally:
        await model.client.close()
