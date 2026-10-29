# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared PostgreSQL controls: operator-owned fresh schemas, no live Bridge/model."""

import asyncio

import httpx2
import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from test_bridge_completion_native import mock_model_requests
from test_execution import seed_repository

from firefly_forge import providers
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.models import RepositoryInput, RunInput


@pytest.mark.parametrize("stream", [False, True], ids=["invalid-json", "empty-stream"])
async def test_unusable_bridge_completion_survives_store_restart_without_resubmission(
    store, store_factory, tmp_path, stream
):
    source = await seed_repository(tmp_path / "source")
    repository = await store.add_repository(RepositoryInput(name="Offline Bridge fixture", url=str(source)))
    run = await store.create_run(
        RunInput(
            repository_id=repository["id"],
            brief="Retain an ambiguous offline completion",
            agent="bridge",
            frontend="none",
            skills=[],
        )
    )
    model = providers.BridgeProvider(
        {"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}
    ).model()
    await model.client._client.aclose()
    submissions = []

    def respond(request):
        submissions.append(request)
        if stream:
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, text="data: [DONE]\n\n")
        return httpx2.Response(200, json={"id": "fictional", "choices": "unusable"})

    model.client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))

    class OfflineBridgeWorker:
        async def execute(self, workspace, *args):
            (workspace / "partial.txt").write_text("Retain partial source until reconciliation")
            with mock_model_requests(model):
                if stream:
                    async with model.request_stream(
                        [ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters()
                    ):
                        pytest.fail("An empty stream cannot provide a completion")
                else:
                    await model.request(
                        [ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters()
                    )

    journal = Journal(store)
    engine = Engine(store, tmp_path / "workspaces", lambda _: OfflineBridgeWorker(), journal=journal)
    try:
        await engine.execute_run(run["id"])
        held = await store.get_run(run["id"])
        entries = await journal.entries(run["id"])
        assert held["status"] == "needs_reconciliation"
        assert held["pause"]["kind"] == "provider_outcome_unknown"
        assert held["pause"]["automatic"] is False and held["pause"]["retry_at"] is None
        intents = [entry for entry in entries if entry["task_id"] == "implement" and entry["kind"] == "intent"]
        errors = [entry for entry in entries if entry["task_id"] == "implement" and entry["kind"] == "error"]
        assert len(intents) == len(errors) == len(submissions) == 1
        assert intents[0]["idempotency_key"] == errors[0]["idempotency_key"]
        assert not any(entry["task_id"] == "implement" and entry["kind"] == "receipt" for entry in entries)
        assert Journal.verify_chain(entries)
        reopened_store = await store_factory(store.path)
        reopened = Journal(reopened_store)
        assert await reopened.recover() == 0
        for agent in (None, "claude"):
            with pytest.raises(ValueError, match="Bridge.*reconcil"):
                await reopened.resume(run["id"], agent)
        await engine.execute_run(run["id"])
        assert len(submissions) == 1
        assert await reopened.entries(run["id"]) == entries
        assert await reopened_store.get_run(run["id"]) == held
        assert (
            engine.workspaces / run["id"] / "partial.txt"
        ).read_text() == "Retain partial source until reconciliation"
        from firefly_forge.controllers import RunService

        run_ids = {value["id"] for value in await store.runs()}
        await engine.cancel_effect(run["id"])
        cancelled = await store.get_run(run["id"])
        assert cancelled["status"] == "cancelled"
        assert cancelled["pause"] == held["pause"]
        with pytest.raises(ValueError, match="Bridge.*reconcil"):
            await RunService(store, engine).retry(run["id"])
        await engine.execute_run(run["id"])
        assert len(submissions) == 1
        assert {value["id"] for value in await store.runs()} == run_ids
        assert await journal.entries(run["id"]) == entries
        assert await store.get_run(run["id"]) == cancelled
    finally:
        await model.client.close()


@pytest.mark.parametrize("kind", ["quota", "provider_paused"])
async def test_known_bridge_pause_remains_manual_after_native_store_restart(store, store_factory, kind):
    repository = await store.add_repository(
        RepositoryInput(name="Offline Bridge fixture", url="https://example.invalid/fictional/repository.git")
    )
    run = await store.create_run(
        RunInput(repository_id=repository["id"], brief="Retain a known Bridge pause", agent="bridge")
    )
    journal = Journal(store)
    effect = await journal.intent(run["id"], "implement")
    observation = {"job_id": "00000000-0000-4000-8000-000000000001", "retry_after": 0}
    error = await journal.fail(
        run["id"], effect["idempotency_key"], kind, "Explicit offline pause", observation=observation
    )
    entries = await journal.entries(run["id"])
    held = await store.get_run(run["id"])
    reopened_store = await store_factory(store.path)
    reopened = Journal(reopened_store)
    assert await reopened.recover() == 0
    with pytest.raises(ValueError, match="explicit operator resume"):
        await reopened.resume(run["id"], auto=True)
    assert await reopened.fail(run["id"], effect["idempotency_key"], kind, "Replay") == error
    assert error["payload"]["provider_observation"] == observation
    assert error["payload"]["automatic"] is False and error["payload"]["retry_at"] is None
    assert await reopened.entries(run["id"]) == entries
    assert await reopened_store.get_run(run["id"]) == held
    assert Journal.verify_chain(entries)


async def test_cancelled_inflight_bridge_submission_survives_restart_without_retry(
    store, store_factory, tmp_path, monkeypatch
):
    from firefly_forge.controllers import RunService

    source = await seed_repository(tmp_path / "source")
    repository = await store.add_repository(RepositoryInput(name="Cancelled Bridge fixture", url=str(source)))
    run = await store.create_run(
        RunInput(repository_id=repository["id"], brief="Retain an in-flight cancellation", agent="bridge", skills=[])
    )
    provider = providers.BridgeProvider(
        {"FORGE_BRIDGE_URL": "http://localhost:19191", "FORGE_BRIDGE_TOKEN": "fictional"}
    )
    model = provider.model()
    await model.client._client.aclose()
    submitted = asyncio.Event()
    blocked = asyncio.Event()
    requests = []

    async def respond(request):
        requests.append(request)
        (engine.workspaces / run["id"] / "partial.txt").write_text("Retain cancelled original work")
        submitted.set()
        await blocked.wait()
        raise AssertionError("This request never returns a response")

    model.client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))

    monkeypatch.setattr(provider, "model", lambda **kwargs: model)
    journal = Journal(store)
    engine = Engine(store, tmp_path / "workspaces", lambda _: provider, journal=journal)
    await store.acquire_ownership()

    async def controlled_execution():
        with mock_model_requests(model):
            await engine.execute_run(run["id"])

    execution = asyncio.create_task(controlled_execution())
    engine.active[run["id"]] = execution
    try:
        waiting = asyncio.create_task(submitted.wait())
        try:
            await asyncio.wait({waiting, execution}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
            observed = await store.get_run(run["id"])
            assert submitted.is_set(), (
                {key: observed.get(key) for key in ("status", "error", "pause", "active_task", "attempt")},
                execution.exception() if execution.done() else None,
            )
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        run_ids = {item["id"] for item in await store.runs()}
        await engine.cancel_effect(run["id"])
        assert execution.done() and execution.cancelled()
        held = await store.get_run(run["id"])
        assert held["status"] == "cancelled"
        assert held["pause"]["kind"] == "provider_outcome_unknown"
        assert held["pause"]["automatic"] is False and held["pause"]["retry_at"] is None
        entries = await journal.entries(run["id"])
        intents = [item for item in entries if item["task_id"] == "implement" and item["kind"] == "intent"]
        errors = [item for item in entries if item["task_id"] == "implement" and item["kind"] == "error"]
        assert len(intents) == len(errors) == len(requests) == 1
        assert intents[0]["idempotency_key"] == errors[0]["idempotency_key"]
        assert errors[0]["payload"]["kind"] == "provider_outcome_unknown"
        assert "provider_observation" not in errors[0]["payload"]
        assert Journal.verify_chain(entries)
        reopened_store = await store_factory(store.path)
        reopened = Journal(reopened_store)
        assert await reopened.recover() == 0
        with pytest.raises(ValueError, match="Bridge.*reconcil"):
            await RunService(reopened_store, engine).retry(run["id"])
        with pytest.raises(ValueError, match="Bridge.*reconcil"):
            await reopened.resume(run["id"])
        assert {item["id"] for item in await reopened_store.runs()} == run_ids
        assert await reopened.entries(run["id"]) == entries
        assert await reopened_store.get_run(run["id"]) == held
        assert len(requests) == 1
        assert (engine.workspaces / run["id"] / "partial.txt").read_text() == "Retain cancelled original work"
    finally:
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        await model.client.close()
