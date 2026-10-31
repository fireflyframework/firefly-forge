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
from firefly_forge.known_worker_completion_controllers import KnownWorkerCompletionController, WorkerCompletionBody
from firefly_forge.known_worker_completions import KnownWorkerCompletions
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied
from firefly_forge.store import Store
from firefly_forge.workspace_tools import WorkspaceTools

pytest_plugins = ["test_invalid_read_continuity_native"]


@pytest.fixture(autouse=True)
def selected_ignored_native_source(request, monkeypatch):
    if (
        getattr(request.node, "callspec", None) is None
        or request.node.callspec.params.get("interrupt") != "receipt_source_race"
    ):
        return
    import test_invalid_read_continuity_native as native_fixture
    from test_checkpoint_source_identity_host import git

    original = native_fixture.seed_restore_workspace

    def seeded(workspace):
        revision = original(workspace)
        asset = workspace / "build" / "asset.txt"
        asset.parent.mkdir()
        asset.write_text("Selected ignored native source\n")
        (workspace / ".git" / "info" / "exclude").write_text("/build/\n")
        git(workspace, "add", "--force", "build/asset.txt")
        return revision

    monkeypatch.setattr(native_fixture, "seed_restore_workspace", seeded)


@pytest.mark.parametrize(
    "interrupt",
    [False, True, "receipt_source_race", "unrequested_qa", "reconcile_held"],
    ids=[
        "accepted_cold_readback",
        "uncertain_after_charge",
        "tracked_ignored_receipt_rollback",
        "unrequested_qa",
        "reconcile_held",
    ],
)
async def test_native_consumed_terminal_same_attempt_acceptance_without_replay(
    native_continuity, monkeypatch, interrupt
):
    import firefly_forge.retained_verified_source as retained

    c = native_continuity
    reconciled = interrupt == "reconcile_held"
    uncertain = interrupt is True
    receipt_race = interrupt == "receipt_source_race"
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

    if interrupt == "unrequested_qa":
        from test_consumed_terminal_completion_host import UNREQUESTED_QA

        report["qa_report"] = copy.deepcopy(UNREQUESTED_QA)

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
    # A combined-service startup must preserve the failed final before any
    # explicit candidate or operator settlement is requested.
    startup_plan = copy.deepcopy(await c.journal.plan(c.run_id))
    # prepared() also owns an unrelated unfinished run. Startup must recover
    # that run while leaving this consumed result byte-exact.
    async with c.store._unit():
        unrelated = [row for row in await c.journal.tasks.find_pending_recovery() if row.run_id.hex != c.run_id]
        assert len(unrelated) == 1 and unrelated[0].task_id == "implement"
        unrelated_id = unrelated[0].run_id.hex
    assert await c.journal.recover() == 1
    assert await c.journal.recover() == 0
    assert (await c.journal.entries(unrelated_id))[-1]["kind"] == "recovery"
    assert await c.store.get_run(c.run_id) == failed
    assert await c.journal.entries(c.run_id) == consumed
    assert await c.journal.plan(c.run_id) == startup_plan
    assert len(fresh_posts) == len(fresh_consumes) == 2
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
            await controller.request(c.run_id, action, WorkerCompletionBody(body))
    assert await c.store.get_run(c.run_id) == failed
    assert await c.journal.entries(c.run_id) == consumed
    reserved = await controller.request(c.run_id, action, WorkerCompletionBody(body))
    assert reserved["state"] == "queued" and reserved["same_task_attempt"] == task_attempt
    assert await controller.request(c.run_id, action, WorkerCompletionBody(body)) == reserved
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
    native_receipt = c.engine._receipt
    before_receipt = []
    if receipt_race:
        from firefly_forge.known_worker_completions import _entries
        from firefly_forge.store import identifier

        async def mutate_inside_receipt(*args, **kwargs):
            before_receipt[:] = _entries(await c.journal.records.find_for_run(identifier(c.run_id)), c.run_id)
            answer = await native_receipt(*args, **kwargs)
            (c.engine.workspaces / c.run_id / "build" / "asset.txt").write_text("Changed during native receipt\n")
            return answer

        monkeypatch.setattr(c.engine, "_receipt", mutate_inside_receipt)
    if uncertain:
        monkeypatch.setattr(c.journal, "worker_feedback", stop_after_charge)
    from firefly_forge.consumed_terminal_completions import ConsumedTerminalExecution

    closed = []
    native_close = ConsumedTerminalExecution.close_snapshot

    def close(execution):
        native_close(execution)
        closed.append(execution._snapshot)

    monkeypatch.setattr(ConsumedTerminalExecution, "close_snapshot", close)
    expected_handoff = before["verification_handoff"]
    if reconciled:
        from types import SimpleNamespace

        import httpx
        from pyfly.container import Container
        from pyfly.web.adapters.starlette.controller import ControllerRegistrar
        from starlette.applications import Starlette
        from test_consumed_terminal_reconciliation_host import reconcile_input

        from firefly_forge.consumed_terminal_completions import claimed_execution
        from firefly_forge.controllers import OperatorErrors

        token = CURRENT_PRINCIPAL.set(None)
        try:
            assert await c.store.claim(c.run_id)
            claimed = await claimed_execution(c.engine, c.run_id)
            await claimed.attest_runner()
        finally:
            CURRENT_PRINCIPAL.reset(token)
        async with c.store._unit():
            row = await c.journal._run(c.run_id)
            expected_handoff = {
                **row.data["verification_handoff"],
                "status": "held",
                "error": "Verification needs explicit reconciliation or renewed authority before continuation.",
            }
            await c.journal._append(
                c.run_id,
                "implement",
                expected_handoff["id"] + ":held",
                "verification_handoff_outcome",
                expected_handoff,
            )
            task = await c.journal._task(c.run_id, "implement")
            task.status = "paused"
            task.data = {key: value for key, value in task.data.items() if key != "verification_handoff"}
            await c.journal.tasks.save(task)
            row.status = "paused"
            row.data = {
                **row.data,
                "status": "paused",
                "display_status": "paused_worker_request",
                "verification_handoff": expected_handoff,
                "error": "Consumed terminal original scope or invocation changed",
            }
            await c.store.run_records.save(row)
        stopped_run, stopped_entries = await c.store.get_run(c.run_id), await c.journal.entries(c.run_id)
        container = Container()
        container.register_instance(KnownWorkerCompletionController, controller)
        container.register_instance(OperatorErrors, OperatorErrors())
        context = SimpleNamespace(container=container, get_bean=container.resolve)
        app = Starlette(routes=ControllerRegistrar().collect_routes(context))
        route = f"/api/v1/runs/{c.run_id}/worker-completion-settlements/{action}"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture.invalid"
        ) as client:
            reviewed = await client.get(route + "/reconciliation-candidate")
            assert reviewed.status_code == 200, reviewed.json()
            reconciliation = reconcile_input(reviewed.json())
            denied = await client.post(
                route + "/reconcile",
                json={**reconciliation.model_dump(mode="json"), "acknowledge_same_claimed_action": 1},
            )
            assert denied.status_code == 422
            assert await c.store.get_run(c.run_id) == stopped_run
            assert await c.journal.entries(c.run_id) == stopped_entries
            save_task = c.journal.tasks.save

            async def rollback_reconciliation(task):
                answer = await save_task(task)
                if task.task_id == "implement" and task.status == "running":
                    raise RuntimeError("Controlled reconciliation atomic rollback")
                return answer

            with monkeypatch.context() as transaction:
                transaction.setattr(c.journal.tasks, "save", rollback_reconciliation)
                with pytest.raises(RuntimeError, match="atomic rollback"):
                    await controller.reconcile(c.run_id, action, reconciliation)
            assert await c.store.get_run(c.run_id) == stopped_run
            assert await c.journal.entries(c.run_id) == stopped_entries
            owner_assert = c.store.assert_ownership
            owner_start = c.store._ownership_backend_start

            async def revoke_after_native_check():
                await owner_assert()
                from firefly_forge.store import identifier

                async with c.store._unit():
                    current = await c.journal.records.find_for_run(identifier(c.run_id))
                if any(e.kind == "consumed_terminal_reconciliation" for e in current):
                    c.store._ownership_backend_start = "1900-01-01 00:00:00+00"

            try:
                with monkeypatch.context() as revoked:
                    revoked.setattr(c.store, "assert_ownership", revoke_after_native_check)
                    with pytest.raises((RuntimeError, AccessDenied)):
                        await controller.reconcile(c.run_id, action, reconciliation)
            finally:
                c.store._ownership_backend_start = owner_start
            assert await c.store.get_run(c.run_id) == stopped_run
            assert await c.journal.entries(c.run_id) == stopped_entries
            from sqlalchemy import text

            owner_identity = c.store.execution_ownership_identity

            async def unlock_after_native_check():
                await owner_assert()
                from firefly_forge.store import identifier

                async with c.store._unit():
                    current = await c.journal.records.find_for_run(identifier(c.run_id))
                if any(e.kind == "consumed_terminal_reconciliation" for e in current):
                    await c.store.execution_ownership_token.scalar(
                        text("SELECT pg_advisory_unlock(hashtextextended(:key,0))"),
                        {"key": "forge:execution:" + c.store.schema},
                    )
                    assert c.store.execution_ownership_identity == owner_identity

            try:
                with monkeypatch.context() as unlocked:
                    unlocked.setattr(c.store, "assert_ownership", unlock_after_native_check)
                    with pytest.raises((RuntimeError, AccessDenied)):
                        await controller.reconcile(c.run_id, action, reconciliation)
            finally:
                connection = c.store.execution_ownership_token
                await connection.scalar(
                    text("SELECT pg_advisory_lock(hashtextextended(:key,0))"),
                    {"key": "forge:execution:" + c.store.schema},
                )
                await connection.commit()
            assert c.store.execution_ownership_identity == owner_identity
            assert await c.store.get_run(c.run_id) == stopped_run
            assert await c.journal.entries(c.run_id) == stopped_entries
            accepted = await client.post(route + "/reconcile", json=reconciliation.model_dump(mode="json"))
            assert accepted.status_code == 200, accepted.json()
            assert accepted.json()["settlement"]["state"] == "accepted"
            assert (await client.get(route + "/reconciliation")).json() == accepted.json()
            assert await controller.reconcile(c.run_id, action, reconciliation) == accepted.json()
        assert (await c.journal.entries(c.run_id))[: len(stopped_entries)] == stopped_entries
        assert sum(e["kind"] == "consumed_terminal_reconciliation" for e in await c.journal.entries(c.run_id)) == 1
    else:
        token = CURRENT_PRINCIPAL.set(None)
        try:
            await asyncio.create_task(c.engine.execute_run(c.run_id))
        finally:
            CURRENT_PRINCIPAL.reset(token)
    assert len(closed) == 1
    if closed[0] is not None:
        assert all(stream.closed for stream in closed[0].source_fence.streams)
    final = await c.store.get_run(c.run_id)
    entries = await c.journal.entries(c.run_id)
    assert final["status"] == ("needs_reconciliation" if uncertain or receipt_race else "paused"), final.get("error")
    assert final["attempt"] == final["implementation_rounds"] == task_attempt
    assert final["implementation_pending"] is False
    assert final["usage"]["runtime_report"] == report
    assert final["usage"]["qa_report"] is None
    assert final["usage"]["browser_evidence"] == []
    for key, delta in (("requests", 2), ("input_tokens", 22), ("output_tokens", 6)):
        assert final["usage"][key] == before["usage"][key] + delta
    assert final["verification_handoff"] == expected_handoff
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
    if receipt_race:
        assert before_receipt
        assert entries == before_receipt
        assert final["consumed_terminal_recovery"]["state"] == "claimed"
        async with c.store._unit():
            task = await c.journal._task(c.run_id, "implement")
            assert task.status == "running"
        assert not [e for e in entries if e["kind"] == "receipt" and e["sequence"] > consumed[-1]["sequence"]]
        # The already-durable feedback/known-output receipt survives; the new
        # implementation receipt and accepted projection roll back together.
        assert [e for e in entries if e["kind"] == "terminal_completion_receipt"] == [
            e for e in before_receipt if e["kind"] == "terminal_completion_receipt"
        ]
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
        assert observed["state"] == ("claimed" if uncertain or receipt_race else "accepted")
        assert await cold.request(c.run_id, action, body) == observed
    finally:
        await reopened.close()
    await c.engine.execute_run(c.run_id)
    assert await c.store.get_run(c.run_id) == final
    assert await c.journal.entries(c.run_id) == entries
    assert len(fresh_posts) == 2

    if not uncertain and not receipt_race and not reconciled:
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
