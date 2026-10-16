# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared PostgreSQL controls: operator-owned fresh schemas, no live Bridge/model."""

import httpx2
import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
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
            if stream:
                async with model.request_stream(
                    [ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters()
                ):
                    pytest.fail("An empty stream cannot provide a completion")
            else:
                await model.request([ModelRequest(parts=[UserPromptPart("Offline")])], None, ModelRequestParameters())

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
