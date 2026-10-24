# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Prepared native SQL restoration controls; controlled backend, no process or provider transport."""

from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic_ai import models
from pyfly.data.transaction import current_unit_of_work
from test_agentic_checkpoint_static_tools_host import GRANTS
from test_agentic_restore_receipt_native import native_restoration
from test_bridge_completion_native import mock_model_requests

from firefly_forge.agentic_checkpoint_invocation import current_checkpoint_invocation
from firefly_forge.process import ProcessResult
from firefly_forge.sandbox import SandboxExecutor
from firefly_forge.workspace_tools import WorkspaceTools


def commands_before_each_write(monkeypatch, *, interrupt_second=False):
    calls = []
    original = WorkspaceTools.write_file

    class Backend:
        async def run(self, config, argv, cwd, timeout, output_limit):
            assert current_unit_of_work() is None
            calls.append(argv)
            if interrupt_second and len(calls) == 2:
                raise RuntimeError("Controlled command interruption after durable start")
            return ProcessResult(argv, 0, "native completed command")

    backend = Backend()

    async def write(tools, path, content):
        invocation = current_checkpoint_invocation()
        assert invocation is not None
        executor = SandboxExecutor(invocation.store, invocation.journal, SimpleNamespace(select=lambda config: backend))
        await executor.execute(invocation.run["id"], tools.root, tools.root, ["controlled", path], 2, "implement")
        return await original(tools, path, content)

    monkeypatch.setattr(WorkspaceTools, "write_file", write)
    return calls


async def test_native_completed_command_graph_restores_then_allocates_new_slot(store, tmp_path, monkeypatch):
    calls = commands_before_each_write(monkeypatch)
    async with native_restoration(store, tmp_path, monkeypatch, grants=sorted(GRANTS), next_tool=True) as env:
        assert calls == [["controlled", "service.py"]]
        before = (env.workspace / "service.py").stat().st_mtime_ns
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.restores.execute(env.request.request_id)
        ticket = await env.requests.get(env.request.request_id)
        assert ticket["status"] == "completed", ticket.get("result")
        assert calls == [["controlled", "service.py"], ["controlled", "later.py"]]
        assert (env.workspace / "service.py").stat().st_mtime_ns == before
        rows = await env.journal.sandbox_executions(env.run_id)
        assert [row["ordinal"] for row in rows] == [1, 2]
        assert all(row["status"] == "removed" for row in rows)
        entries = await env.journal.entries(env.run_id)
        assert env.journal.verify_chain(entries)
        assert sum(row["kind"] == "agent_consume" for row in entries) == 3
        assert len(env.posts) == 3 and env.checks == [1]
        assert env.schemas and set(env.schemas) == {store.schema}


@pytest.mark.parametrize("drift", ["unknown", "cancel", "grants"])
async def test_native_completed_command_restore_refuses_late_custody_or_authority_drift(
    store, tmp_path, monkeypatch, drift
):
    calls = commands_before_each_write(monkeypatch)
    async with native_restoration(store, tmp_path, monkeypatch, grants=sorted(GRANTS), next_tool=True) as env:
        if drift == "unknown":
            async with store._unit():
                row = (await env.journal.sandboxes.for_run(UUID(env.run_id)))[0]
                row.data = {**row.data, "effects": {**row.data["effects"], "start": {"status": "intent"}}}
                await env.journal.sandboxes.save(row)
        elif drift == "cancel":
            await store.update(env.run_id, cancel_request={"status": "requested"})
        else:
            run = await store.get_run(env.run_id)
            roles = run["role_snapshot"]
            roles["digest"] = "0" * 64
            await store.update(env.run_id, role_snapshot=roles)
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.restores.execute(env.request.request_id)
        ticket = await env.requests.get(env.request.request_id)
        assert ticket["status"] == "failed"
        assert calls == [["controlled", "service.py"]]
        assert len(env.posts) == 2
        entries = await env.journal.entries(env.run_id)
        assert sum(row["kind"] == "agent_consume" for row in entries) == 1
        assert env.journal.verify_chain(entries)


async def test_native_new_interrupted_command_stays_unknown_without_replay(store, tmp_path, monkeypatch):
    calls = commands_before_each_write(monkeypatch, interrupt_second=True)
    async with native_restoration(store, tmp_path, monkeypatch, grants=sorted(GRANTS), next_tool=True) as env:
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.restores.execute(env.request.request_id)
        ticket = await env.requests.get(env.request.request_id)
        assert ticket["status"] == "needs_reconciliation"
        assert calls == [["controlled", "service.py"], ["controlled", "later.py"]]
        assert len(env.posts) == 2
        rows = await env.journal.sandbox_executions(env.run_id)
        assert [row["ordinal"] for row in rows] == [1, 2]
        assert rows[1]["effects"]["start"]["status"] == "intent"
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.restores.execute(env.request.request_id)
        assert len(calls) == 2 and len(env.posts) == 2
        assert env.journal.verify_chain(await env.journal.entries(env.run_id))
