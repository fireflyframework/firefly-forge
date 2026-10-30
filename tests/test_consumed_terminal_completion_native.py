# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared PostgreSQL recovery of a consumed result, with controlled SDK transport."""

import asyncio
import copy
import json
from uuid import uuid4

import httpx2
import pytest
from test_agentic_checkpoint_activation_host import result
from test_bridge_completion_native import mock_model_requests
from test_consumed_terminal_completion_host import consent
from test_final_outcome_continuation_native import (
    test_native_consumed_final_acceptance_releases_existing_verification_without_replay as accept_and_verify,
)

from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.journal import Journal
from firefly_forge.known_worker_completion_controllers import KnownWorkerCompletionController
from firefly_forge.known_worker_completions import KnownWorkerCompletions
from firefly_forge.project_access import CURRENT_PRINCIPAL
from firefly_forge.store import Store
from firefly_forge.workspace_tools import WorkspaceTools

pytest_plugins = ["test_invalid_read_continuity_native"]


@pytest.mark.parametrize("interrupt", [False, True], ids=["accepted_cold_readback", "uncertain_after_charge"])
async def test_native_consumed_terminal_same_attempt_acceptance_without_replay(
    native_continuity, monkeypatch, interrupt
):
    import firefly_forge.retained_verified_source as retained

    c = native_continuity
    provider = c.engine.provider_factory("bridge")
    execute, consume, read = provider.execute, AgentCheckpoints._consume, WorkspaceTools.read_file
    source_prepare = retained.prepare_retained_source
    await accept_and_verify(c, monkeypatch)
    before = copy.deepcopy(await c.store.get_run(c.run_id))
    prefix = await c.journal.entries(c.run_id)
    commands = await c.journal.sandbox_executions(c.run_id)
    old_consumes = {e["idempotency_key"] for e in prefix if e["kind"] == "agent_consume"}
    old_writes = copy.deepcopy(c.checkpoint_writes)
    model = provider.model()
    original_transport = model.client._client._transport
    fresh_posts, fresh_consumes = [], []
    report = dict(
        summary="The unchanged source retains its completed native verification",
        worker_feedback=dict(
            id="native-consumed-final",
            role_id="implementer",
            task_id="implement",
            attempt=before["attempt"] + 1,
            disposition="completed",
            summary="The existing verification is retained",
            evidence_paths=[],
            request=None,
        ),
        qa_report=None,
    )

    def transport(request):
        if request.method == "GET":
            return original_transport.handler(request)
        fresh_posts.append(json.loads(request.content))
        assert len(fresh_posts) <= 2, "A consumed result cannot be requested again"
        body = result(tool=True) if len(fresh_posts) == 1 else result(json.dumps(report))
        if len(fresh_posts) == 1:
            body["choices"][0]["message"]["tool_calls"][0]["function"].update(
                name="read_file", arguments='{"path":"existing.py"}'
            )
        return httpx2.Response(200, json=body)

    async def capture_consume(self, run, intent, body, current, reader):
        assert intent["idempotency_key"] not in old_consumes
        fresh_consumes.append(intent["idempotency_key"])
        return await consume(self, run, intent, body, current, reader)

    async def stop_before_agent_result(*args, **kwargs):
        assert CURRENT_PRINCIPAL.get() is None
        raise RuntimeError("Controlled interruption after final consume before AgentResult")

    monkeypatch.setattr(provider, "execute", execute)
    monkeypatch.setattr(AgentCheckpoints, "_consume", capture_consume)
    monkeypatch.setattr(WorkspaceTools, "read_file", read)
    monkeypatch.setattr(model.client._client, "_transport", httpx2.MockTransport(transport))
    with monkeypatch.context() as failure:
        failure.setattr(retained, "prepare_retained_source", stop_before_agent_result)
        token = CURRENT_PRINCIPAL.set(None)
        try:
            with mock_model_requests(model):
                await asyncio.create_task(c.engine.execute_run(c.run_id))
        finally:
            CURRENT_PRINCIPAL.reset(token)
    assert retained.prepare_retained_source is source_prepare
    failed = await c.store.get_run(c.run_id)
    consumed = await c.journal.entries(c.run_id)
    assert failed["status"] == "failed" and failed["implementation_pending"] is True
    assert failed["attempt"] == before["attempt"] + 1
    assert failed["usage"] == before["usage"]
    assert consumed[-1]["kind"] == "agent_consume" and len(fresh_consumes) == len(fresh_posts) == 2
    assert consumed[: len(prefix)] == prefix
    task_attempt = failed["attempt"]
    api = KnownWorkerCompletions(c.store, c.journal, c.engine)
    controller = KnownWorkerCompletionController(api)
    candidate = await controller.candidate(c.run_id)
    body, action = consent(candidate), uuid4().hex
    save = c.store.run_records.save

    async def rollback(row):
        answer = await save(row)
        if row.data.get("consumed_terminal_recovery"):
            raise RuntimeError("Controlled native recovery projection rollback")
        return answer

    with monkeypatch.context() as transaction:
        transaction.setattr(c.store.run_records, "save", rollback)
        with pytest.raises(RuntimeError, match="projection rollback"):
            await controller.request(c.run_id, action, body)
    assert await c.store.get_run(c.run_id) == failed
    assert await c.journal.entries(c.run_id) == consumed
    reserved = await controller.request(c.run_id, action, body)
    assert reserved["state"] == "queued" and reserved["same_task_attempt"] == task_attempt
    assert await controller.request(c.run_id, action, body) == reserved
    assert (await c.store.get_run(c.run_id))["usage"] == before["usage"]

    async def forbidden(*args, **kwargs):
        raise AssertionError("Consumed terminal recovery cannot replay a provider, tool or native check")

    async def stop_after_charge(*args, **kwargs):
        raise RuntimeError("Controlled interruption after durable usage before feedback")

    monkeypatch.setattr(provider, "execute", forbidden)
    monkeypatch.setattr(AgentCheckpoints, "_consume", forbidden)
    monkeypatch.setattr(WorkspaceTools, "read_file", forbidden)
    monkeypatch.setattr(WorkspaceTools, "write_file", forbidden)
    monkeypatch.setattr(c.engine, "_project_command", forbidden)
    monkeypatch.setattr(c.engine, "_independent_review", forbidden)
    if interrupt:
        monkeypatch.setattr(c.journal, "worker_feedback", stop_after_charge)
    token = CURRENT_PRINCIPAL.set(None)
    try:
        await asyncio.create_task(c.engine.execute_run(c.run_id))
    finally:
        CURRENT_PRINCIPAL.reset(token)
    final = await c.store.get_run(c.run_id)
    entries = await c.journal.entries(c.run_id)
    assert final["status"] == ("needs_reconciliation" if interrupt else "paused"), final.get("error")
    assert final["attempt"] == final["implementation_rounds"] == task_attempt
    assert final["implementation_pending"] is False
    assert final["usage"]["runtime_report"] == report
    for key, delta in (("requests", 2), ("input_tokens", 22), ("output_tokens", 6)):
        assert final["usage"][key] == before["usage"][key] + delta
    assert final["verification_handoff"] == before["verification_handoff"]
    assert await c.journal.sandbox_executions(c.run_id) == commands
    assert c.checkpoint_writes == old_writes
    assert entries[: len(consumed)] == consumed and Journal.verify_chain(entries)
    for kind in ("consumed_terminal_claim", "consumed_terminal_result"):
        assert sum(e["kind"] == kind for e in entries) == 1
    assert {e["idempotency_key"] for e in entries if e["kind"] == "agent_consume"} == old_consumes | set(fresh_consumes)
    assert sum(e["kind"] == "agentic_tool_intent" for e in entries) == sum(
        e["kind"] == "agentic_tool_intent" for e in consumed
    )
    assert len(fresh_posts) == len(fresh_consumes) == 2
    reopened = Store(c.store.path, schema=c.store.schema, session_provider=c.store.session_provider)
    try:
        await reopened.initialize(existing_only=True)
        cold_journal = Journal(reopened)
        registry = reopened.session_provider._registry()
        assert registry is not None
        for repository in (cold_journal.records, cold_journal.tasks, cold_journal.sandboxes):
            repository._bind_transaction_managers(registry)
        cold = KnownWorkerCompletions(reopened, cold_journal)
        observed = await cold.get(c.run_id, action)
        assert observed["state"] == ("claimed" if interrupt else "accepted")
        assert await cold.request(c.run_id, action, body) == observed
    finally:
        await reopened.close()
    await c.engine.execute_run(c.run_id)
    assert await c.store.get_run(c.run_id) == final
    assert await c.journal.entries(c.run_id) == entries
    assert len(fresh_posts) == 2

    if not interrupt:
        await c.journal.resume(c.run_id)
        checkpoints = []

        async def next_verification(*args, **kwargs):
            checkpoints.append(True)
            raise RuntimeError("Controlled ordinary verification frontier reached")

        monkeypatch.setattr(c.engine, "_project_command", next_verification)
        token = CURRENT_PRINCIPAL.set(None)
        try:
            await asyncio.create_task(c.engine.execute_run(c.run_id))
        finally:
            CURRENT_PRINCIPAL.reset(token)
        continued = await c.store.get_run(c.run_id)
        assert checkpoints == [True], continued.get("error")
        assert continued["usage"] == final["usage"] and continued["attempt"] == final["attempt"]
        assert len(fresh_posts) == 2
        assert sum(e["kind"] == "consumed_terminal_claim" for e in await c.journal.entries(c.run_id)) == 1
