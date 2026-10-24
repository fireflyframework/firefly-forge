# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual command executor/Journal controls with transactional memory rows, not SQL proof."""

import copy
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_sandbox_command_ordinals_host import command

from firefly_forge.agent_checkpoint_models import InvocationBinding
from firefly_forge.agent_restores import AgentRestores

pytest_plugins = ["test_sandbox_command_ordinals_host"]


async def retained(case):
    await command(case, ["completed-before-checkpoint"])
    context = InvocationBinding(
        invocation_id="a" * 32,
        ordinal=2,
        predecessor="b" * 64,
        brief="Continue after the completed command without replay",
        backend="pyfly",
        frontend="none",
        round=1,
        deadline_ms=int(time.time() * 1000) + 60_000,
        grants=["execute_command"],
        contract="c" * 64,
        run_cut="d" * 64,
        workspace={"path": str(case.workspace), "head": "e" * 40, "metadata": "f" * 64, "source": "1" * 64},
    )
    checkpoint = await case.journal._append(
        case.run_id,
        "implement",
        "fbr1_" + "2" * 32,
        "agent_checkpoint",
        {"invocation": context.model_dump(mode="json"), "intent_digest": "9" * 64},
    )
    service = AgentRestores(case.store, SimpleNamespace(), SimpleNamespace(journal=case.journal))
    return service, context, checkpoint


async def test_completed_native_command_can_restore_without_replay_or_counter_reset(command_case):
    case = command_case
    service, context, _ = await retained(case)
    await service._command_restore(await case.store.get_run(case.run_id), context)
    assert case.calls == [["completed-before-checkpoint"]]
    await command(case, ["new-after-restoration"])
    records = await case.journal.sandbox_executions(case.run_id)
    assert [row["ordinal"] for row in records] == [1, 2]
    assert case.calls == [["completed-before-checkpoint"], ["new-after-restoration"]]


async def consumed(case, checkpoint):
    ticket = {"id": "3" * 32, "generation": "4" * 32}
    await case.journal._append(
        case.run_id,
        "implement",
        checkpoint["idempotency_key"],
        "agent_consume",
        {
            "version": 2,
            "request_id": checkpoint["idempotency_key"],
            "intent_digest": checkpoint["payload"]["intent_digest"],
            "checkpoint_record_digest": checkpoint["digest"],
            "source": {"kind": "http_response", "record_digest": "5" * 64},
            "response_sha256": "6" * 64,
            "response_bytes": 1,
            "restore_request_id": ticket["id"],
            "generation": ticket["generation"],
        },
    )
    return ticket


async def test_reentry_accepts_only_new_complete_effects_after_matching_consume(command_case):
    case = command_case
    service, context, checkpoint = await retained(case)
    ticket = await consumed(case, checkpoint)
    await command(case, ["new-after-consumption"])
    await service._command_restore(await case.store.get_run(case.run_id), context, ticket=ticket)
    assert case.calls == [["completed-before-checkpoint"], ["new-after-consumption"]]


async def test_completed_effect_after_checkpoint_before_consume_is_not_restorable(command_case):
    case = command_case
    service, context, checkpoint = await retained(case)
    await command(case, ["effect-outside-retained-history"])
    ticket = await consumed(case, checkpoint)
    with pytest.raises(ValueError, match="Prior runner"):
        await service._command_restore(await case.store.get_run(case.run_id), context, ticket=ticket)
    assert len(case.calls) == 2


@pytest.mark.parametrize("field", ["generation", "id"])
async def test_reentry_refuses_another_restore_ticket_or_generation(command_case, field):
    case = command_case
    service, context, checkpoint = await retained(case)
    ticket = await consumed(case, checkpoint)
    await command(case)
    ticket[field] = "7" * 32
    with pytest.raises(ValueError, match="Prior runner"):
        await service._command_restore(await case.store.get_run(case.run_id), context, ticket=ticket)
    assert len(case.calls) == 2


@pytest.mark.parametrize(
    "change",
    [
        "unknown",
        "interrupted",
        "unreconciled",
        "ordinal",
        "attempt",
        "snapshot",
        "legacy",
        "output",
        "orphan_row",
        "orphan_wal",
        "chain",
        "budget",
    ],
)
async def test_unknown_forged_or_orphan_command_custody_refuses_without_dispatch(command_case, change):
    case = command_case
    service, context, _ = await retained(case)
    row = case.store.sandboxes.rows[0]
    before = copy.deepcopy(case.calls)
    if change == "unknown":
        row.data["effects"]["start"]["status"] = "intent"
    elif change == "interrupted":
        row.data["effects"]["start"]["evidence"]["outcome"] = "interrupted"
    elif change == "unreconciled":
        row.data["effects"]["start"]["evidence"]["workspace_import"] = "requires_reconciliation"
    elif change == "ordinal":
        row.ordinal += 1
    elif change == "attempt":
        row.parent_attempt += 1
    elif change == "snapshot":
        row.snapshot_digest = "7" * 64
    elif change == "legacy":
        row.data["request"].pop("executor_contract")
    elif change == "output":
        row.data["effects"]["start"]["evidence"]["output"] = "forged"
    elif change == "orphan_row":
        case.store.sandboxes.rows.clear()
    elif change == "orphan_wal":
        row.id = uuid4()
    elif change == "chain":
        case.store.records.rows[0].digest = "0" * 64
    else:
        case.store.run.data["sandbox_snapshot"]["configuration"]["command_limit"] = 0
    with pytest.raises(ValueError):
        await service._command_restore(await case.store.get_run(case.run_id), context)
    assert case.calls == before


async def test_exhausted_command_budget_stays_spent_after_permitted_restoration(command_case):
    from firefly_forge.sandbox_models import SandboxInput, capture_sandbox

    case = command_case
    case.store.run.data["sandbox_snapshot"] = capture_sandbox(SandboxInput(command_limit=1))
    service, context, _ = await retained(case)
    await service._command_restore(await case.store.get_run(case.run_id), context)
    with pytest.raises(ValueError, match="budget"):
        await command(case, ["over-budget"])
    assert case.calls == [["completed-before-checkpoint"]]


async def test_actual_sdk_graph_retains_complete_command_then_allocates_after_restore(tmp_path, monkeypatch):
    from pydantic_ai import models
    from test_agentic_checkpoint_activation_host import RUN, activated, controlled_source_snapshot, queue_original
    from test_agentic_checkpoint_static_tools_host import set_command_grants
    from test_bridge_completion_native import mock_model_requests

    from firefly_forge import engine as engine_module
    from firefly_forge.process import ProcessResult
    from firefly_forge.sandbox import SandboxExecutor
    from firefly_forge.workspace_tools import WorkspaceTools

    env = await activated(tmp_path, monkeypatch)
    set_command_grants(env, tmp_path)
    dispatched = []

    class Backend:
        async def run(self, config, argv, cwd, timeout, output_limit):
            dispatched.append(argv)
            return ProcessResult(argv, 0, "completed")

    backend = Backend()
    original = WorkspaceTools.write_file

    async def write(tools, path, content):
        executor = SandboxExecutor(env.store, env.journal, SimpleNamespace(select=lambda config: backend))
        await executor.execute(RUN, tools.root, tools.root, ["controlled", path], 2, "implement")
        return await original(tools, path, content)

    monkeypatch.setattr(WorkspaceTools, "write_file", write)
    try:
        posts, _, _, _, request = await queue_original(env, monkeypatch, source="http_response", next_tool=True)
        assert dispatched == [["controlled", "service.py"]]
        before = (env.workspace / "service.py").stat().st_mtime_ns
        checked = engine_module.checked

        async def head(argv, cwd, **kwargs):
            if argv == ["git", "rev-parse", "HEAD"]:
                return "b" * 40
            return await checked(argv, cwd, **kwargs)

        async def snapshot(*args, **kwargs):
            return ["service.py", "later.py"], controlled_source_snapshot("Retained command graph"), "c" * 40

        async def checkpoint(*args, **kwargs):
            await env.store.update(RUN, checkpoint_revision="d" * 40)

        async def artifact(*args, **kwargs):
            await env.store.update(RUN, artifacts=[])

        monkeypatch.setattr(engine_module, "checked", head)
        monkeypatch.setattr(env.engine, "_snapshot", snapshot)
        monkeypatch.setattr(env.engine, "_checkpoint_commit", checkpoint)
        monkeypatch.setattr(env.engine, "_artifact", artifact)
        # A third model result is a separately admitted new call after restoring the second result.
        import httpx2
        from test_agentic_checkpoint_activation_host import next_result
        from test_bridge_completion_callbacks import CAPABILITY

        def transport(request):
            if request.method == "GET":
                return httpx2.Response(200, json={"completionRecovery": CAPABILITY})
            posts.append(request.content)
            return httpx2.Response(200, json=next_result(False))

        env.model.client._client._transport = httpx2.MockTransport(transport)
        with models.override_allow_model_requests(False), mock_model_requests(env.model):
            await env.restores.execute(request.request_id)
        ticket = await env.requests.get(request.request_id)
        assert ticket["status"] == "completed", ticket.get("result")
        assert dispatched == [["controlled", "service.py"], ["controlled", "later.py"]]
        assert (env.workspace / "service.py").stat().st_mtime_ns == before
        assert len(posts) == 3
        assert [row["ordinal"] for row in await env.journal.sandbox_executions(RUN)] == [1, 2]
        entries = [row.data for row in env.journal.records.rows]
        assert env.journal.verify_chain(entries)
        assert sum(row["kind"] == "agent_consume" for row in entries) == 3
    finally:
        await env.model.client.close()


def test_restore_code_bytes_are_part_of_checkpoint_runtime_contract(monkeypatch):
    from pathlib import Path

    from firefly_forge.agentic_checkpoint_invocation import runtime_contract

    before = runtime_contract()
    original = Path.read_bytes

    def changed(path):
        value = original(path)
        return value + b"\n# controlled restore semantic drift" if path.name == "agent_restores.py" else value

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert runtime_contract() != before
