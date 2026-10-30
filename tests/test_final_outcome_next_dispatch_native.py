# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fictional native final acceptance followed by an ordinary verified worker dispatch."""

import asyncio
import copy
import json
from functools import wraps
from uuid import UUID

import httpx2
import pytest
from test_agentic_checkpoint_activation_host import result
from test_bridge_completion_native import mock_model_requests
from test_final_outcome_continuation_native import (
    test_native_consumed_final_acceptance_releases_existing_verification_without_replay as accept_and_verify,
)

from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.journal import Journal
from firefly_forge.project_access import CURRENT_PRINCIPAL
from firefly_forge.workspace_tools import WorkspaceTools

pytest_plugins = ["test_invalid_read_continuity_native"]


@pytest.mark.parametrize(
    "scenario", ["success", "qa_red", "source_edit", "actor_revoked", "owner_revoked", "late_source_edit"]
)
async def test_native_known_final_verification_queues_a_real_next_dispatch_without_prior_replay(
    native_continuity, monkeypatch, scenario
):
    c = native_continuity
    qa_red = scenario == "qa_red"
    provider = c.engine.provider_factory("bridge")
    execute, consume = provider.execute, AgentCheckpoints._consume
    await c.store.update(c.run_id, repair_budget=1)
    await accept_and_verify(c, monkeypatch)
    before_run = copy.deepcopy(await c.store.get_run(c.run_id))
    prefix = copy.deepcopy(await c.journal.entries(c.run_id))
    commands = copy.deepcopy(await c.journal.sandbox_executions(c.run_id))
    assert before_run["status"] == "queued" and before_run["attempt"] == 2
    old_ids = {e["idempotency_key"] for e in prefix if e["kind"] == "agent_consume"}
    old_writes, old_reads = copy.deepcopy(c.checkpoint_writes), copy.deepcopy(c.checkpoint_reads)
    workspace = c.engine.workspaces / c.run_id
    if scenario == "owner_revoked":
        from firefly_forge.retained_verified_source import prepare_retained_source

        await c.store.release_ownership()
        with pytest.raises(RuntimeError, match="ownership"):
            await prepare_retained_source(c.store, c.journal, before_run, workspace)
        assert await c.journal.entries(c.run_id) == prefix
        assert await c.journal.sandbox_executions(c.run_id) == commands
        assert len(c.posts) == 4
        return
    if scenario == "source_edit":
        (workspace / "existing.py").write_text("VALUE = 99\n")
    admit = c.store.admit_credentials
    actor_id = c.actor.id

    async def late_change(value):
        await admit(value)
        if (
            scenario == "actor_revoked"
            and isinstance(value, dict)
            and value.get("summary") == "Fresh worker accepted the retained verification evidence"
        ):
            from firefly_forge.identity_persistence import ForgeIdentityUserRepository

            async with c.store._unit():
                row = await ForgeIdentityUserRepository().find_by_id(actor_id)
                assert row is not None and row.enabled
                row.enabled = False
                await ForgeIdentityUserRepository().save(row)
        if (
            scenario == "late_source_edit"
            and isinstance(value, dict)
            and set(value) == {"report", "payload", "checkpoint"}
        ):
            (workspace / "existing.py").write_text("VALUE = 99\n")

    monkeypatch.setattr(c.store, "admit_credentials", late_change)
    model = provider.model()
    original = model.client._client._transport
    fresh_posts, fresh_consumes, dispatches = [], [], []

    def transport(request):
        if request.method == "GET":
            return original.handler(request)
        fresh_posts.append(json.loads(request.content))
        assert len(fresh_posts) == 1, "One ordinary new worker turn, never a prior request"
        return httpx2.Response(
            200,
            json=result(
                json.dumps(
                    dict(
                        summary="Fresh worker accepted the retained verification evidence",
                        worker_feedback=None,
                        qa_report=None,
                    )
                )
            ),
        )

    @wraps(consume)
    async def consume_new(self, run, intent, body, current, reader):
        assert intent["idempotency_key"] not in old_ids, "A previously consumed response cannot run again"
        fresh_consumes.append(intent["idempotency_key"])
        return await consume(self, run, intent, body, current, reader)

    @wraps(execute)
    async def execute_new(*args, **kwargs):
        dispatches.append(True)
        return await execute(*args, **kwargs)

    def refuse_callback(original_callback):
        @wraps(original_callback)
        async def refused(*args, **kwargs):
            raise AssertionError("The fresh final report cannot replay an old read or write")

        return refused

    if qa_red:
        from firefly_forge.process import ProcessResult

        class FailedCheck:
            async def run(self, config, argv, cwd, timeout, output_limit):
                c.command_calls.append(argv)
                return ProcessResult(argv, 1, "Controlled real native QA failure")

        monkeypatch.setattr(c.engine.sandbox.backends, "select", lambda _: FailedCheck())
    monkeypatch.setattr(provider, "execute", execute_new)
    monkeypatch.setattr(AgentCheckpoints, "_consume", consume_new)
    monkeypatch.setattr(WorkspaceTools, "read_file", refuse_callback(WorkspaceTools.read_file))
    monkeypatch.setattr(WorkspaceTools, "write_file", refuse_callback(WorkspaceTools.write_file))
    monkeypatch.setattr(model.client._client, "_transport", httpx2.MockTransport(transport))

    async def background_worker():
        assert CURRENT_PRINCIPAL.get() is None
        await c.engine.execute_run(c.run_id)
        assert CURRENT_PRINCIPAL.get() is None

    reader = CURRENT_PRINCIPAL.set(None)
    try:
        with mock_model_requests(model):
            await asyncio.create_task(background_worker())
    finally:
        CURRENT_PRINCIPAL.reset(reader)
    final = await c.store.get_run(c.run_id)
    if scenario == "actor_revoked":
        from firefly_forge.known_worker_completions import _entries
        from firefly_forge.project_access import AccessDenied

        with pytest.raises(AccessDenied):
            await c.journal.entries(c.run_id)
        async with c.store._unit():
            entries = _entries(await c.journal.records.find_for_run(UUID(c.run_id)), c.run_id)
    else:
        entries = await c.journal.entries(c.run_id)
    if scenario in {"source_edit", "actor_revoked", "late_source_edit"}:
        assert final["status"] in {"failed", "needs_reconciliation"}, final.get("error")
        if scenario == "late_source_edit":
            assert final["bridge_completion_hold"]
        else:
            assert final.get("error")
        async with c.store._unit():
            unfinished = await c.journal._task(c.run_id, "implement")
            assert unfinished.status != "done"
        assert entries[: len(prefix)] == prefix
        assert sum(e["kind"] == "terminal_completion_receipt" for e in entries) == 1
        expected_calls = 0 if scenario == "source_edit" else 1
        assert len(fresh_posts) == len(fresh_consumes) == len(dispatches) == expected_calls, final.get("error")
        assert c.checkpoint_reads == old_reads and c.checkpoint_writes == old_writes
        assert await c.journal.sandbox_executions(c.run_id) == commands
        assert final["attempt"] == 3 and final["repair_budget"] == 1
        return
    assert final["status"] == ("failed" if qa_red else "succeeded"), final.get("error")
    assert final["attempt"] == final["implementation_rounds"] == 3
    assert final["repair_budget"] == before_run["repair_budget"]
    assert final["sandbox_snapshot"] == before_run["sandbox_snapshot"]
    assert entries[: len(prefix)] == prefix and Journal.verify_chain(entries)
    assert len(fresh_posts) == len(fresh_consumes) == len(dispatches) == 1
    assert c.checkpoint_writes == old_writes and c.checkpoint_reads == old_reads
    assert len(await c.journal.sandbox_executions(c.run_id)) == len(commands) + 1
    if qa_red:
        assert final["verification"][-1]["exit_code"] == 1
        assert "Verification failed after 3 implementation attempts" in final["error"]
    else:
        assert final["final_verification"][0]["exit_code"] == 0
    assert final["usage"]["files_written"] == []
    retained = final["usage"]["retained_source"]
    assert retained["parent_attempt"] == 2 and retained["attempt"] == 3
    assert retained["request_id"] == before_run["invalid_read_continuity"]["request_id"]
    from firefly_forge.native_terminal_completion_models import validate_native_receipt

    terminal = [e for e in entries if e["kind"] == "terminal_completion_receipt"]
    assert len(terminal) == 2 and terminal[-1]["payload"]["proof"]["retained_source"] == retained
    validate_native_receipt(terminal[-1], entries)
    from firefly_forge.final_outcome_models import RetainedVerifiedSourceV1
    from firefly_forge.journal import digest
    from firefly_forge.retained_verified_source import validate_retained_source_receipt

    for field in ("source_digest", "commands_digest", "scope_digest", "final_outcome_witness_digest"):
        altered = {**retained, field: "f" * 64}
        altered["digest"] = digest({k: v for k, v in altered.items() if k != "digest"})
        typed = RetainedVerifiedSourceV1.model_validate(altered)
        with pytest.raises(ValueError):
            validate_retained_source_receipt(typed, terminal[-1], entries)
    assert (c.engine.workspaces / c.run_id / "existing.py").read_text() == "VALUE = 2\n"
    async with c.store._unit():
        assert await c.requests.records.count_for_run(UUID(c.run_id), "invalid_read") == 1
    assert sum(e["kind"] == "agent_consume" for e in entries) == len(old_ids) + 1
