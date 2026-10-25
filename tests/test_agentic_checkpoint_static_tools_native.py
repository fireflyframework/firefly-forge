# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real SQL checkpoint custody and durable refusal; no external models or commands."""

from uuid import uuid4

import pytest
from pydantic_ai import models
from test_agentic_checkpoint_static_tools_host import GRANTS
from test_agentic_restore_receipt_native import native_restoration
from test_bridge_completion_native import mock_model_requests

from firefly_forge.agentic_checkpoint_invocation import current_checkpoint_invocation
from firefly_forge.workspace_tools import WorkspaceTools


async def test_native_static_tools_restore_original_result_when_no_runner_effect_exists(store, tmp_path, monkeypatch):
    async with native_restoration(store, tmp_path, monkeypatch, grants=sorted(GRANTS)) as env:
        assert await env.journal.sandbox_executions(env.run_id) == []
        before = (env.workspace / "service.py").stat().st_mtime_ns
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.restores.execute(env.request.request_id)
        ticket = await env.restores.get(env.run_id, env.intent["idempotency_key"], env.request.request_id)
        assert ticket["status"] == "completed"
        assert len(env.posts) == 2 and env.checks == [1]
        assert (env.workspace / "service.py").stat().st_mtime_ns == before
        entries = await env.journal.entries(env.run_id)
        assert env.journal.verify_chain(entries)
        assert sum(record["kind"] == "agent_consume" for record in entries) == 2


@pytest.mark.parametrize("outcome", ["unknown", "done", "interrupted"])
async def test_native_runner_history_blocks_restore_after_consumed_tool_without_reset(
    store, tmp_path, monkeypatch, outcome
):
    original = WorkspaceTools.write_file
    retained = {}

    async def record_before_write(tools, path, content):
        invocation = current_checkpoint_invocation()
        assert invocation is not None
        journal, run_id = invocation.journal, invocation.run["id"]
        before = await journal.entries(run_id)
        assert sum(record["kind"] == "agent_consume" for record in before) == 1
        execution_id = uuid4().hex
        payload = {
            "parent_attempt": 1,
            "ordinal": 1,
            "container_name": "forge-fixture-" + execution_id,
            "snapshot_digest": "a" * 64,
            "request_digest": "b" * 64,
        }
        result = await journal.sandbox_effect_intent(
            run_id, "implement", execution_id, effect="create", payload=payload
        )
        assert result["disposition"] == "execute"
        if outcome != "unknown":
            await journal.sandbox_effect_receipt(
                run_id, "implement", execution_id, effect="create", evidence={"outcome": outcome}
            )
        retained.update(journal=journal, run_id=run_id, execution_id=execution_id)
        return await original(tools, path, content)

    monkeypatch.setattr(WorkspaceTools, "write_file", record_before_write)
    with pytest.raises(ValueError, match="Prior runner.*restore"):
        async with native_restoration(store, tmp_path, monkeypatch, grants=sorted(GRANTS)):
            pytest.fail("Prior command custody must prevent a restore ticket")
    journal, run_id = retained["journal"], retained["run_id"]
    records = await journal.sandbox_executions(run_id)
    assert len(records) == 1 and records[0]["execution_id"] == retained["execution_id"]
    entries = await journal.entries(run_id)
    assert journal.verify_chain(entries)
    assert sum(record["kind"] == "agent_consume" for record in entries) == 1
    assert sum(record["kind"] == "agent_checkpoint" for record in entries) == 2
