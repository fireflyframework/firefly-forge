# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fictional PostgreSQL cases for same-ticket checkpoint continuation.

These tests use Store/Journal/request repositories, public mock model transports
and confined source callbacks. They supply no actual-provider or retained-CRM evidence.
"""

import asyncio
import copy
from uuid import uuid4

import pytest
from checkpoint_continuation_native_fixtures import (
    PENDING_CALL,
    WRITE_CALL,
    checkpoint_input,
    enable_checkpoint_continuation,
    finish_checkpoint_transport,
    native_checkpoint_cut,
    seed_pending_checkpoint,
)
from test_bridge_completion_native import mock_model_requests

from firefly_forge.agent_checkpoint_models import CheckpointPayloadV2
from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint
from firefly_forge.checkpoint_continuations import ADMITTED, HANDOFF
from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy

pytest_plugins = ["test_invalid_read_continuity_native"]


async def test_native_checkpoint_consumes_first_pending_response_once_and_preserves_completed_write(
    native_continuity, monkeypatch
):
    c = native_continuity
    failed = await seed_pending_checkpoint(c, monkeypatch)
    before = await native_checkpoint_cut(c, failed["id"])
    original_run = copy.deepcopy(await c.store.get_run(c.run_id))
    pending = before["journal"][-1]
    held = await c.service.checkpoints.candidate(c.run_id, failed["id"])
    assert held["admission"] == "HOLD" and "checkpoint_continuation_disabled" in held["holds"]
    assert await native_checkpoint_cut(c, failed["id"]) == before
    enable_checkpoint_continuation(c)
    candidate = await c.service.checkpoints.candidate(c.run_id, failed["id"])
    assert candidate["admission"] == "READY", candidate["holds"]
    assert candidate["attempt"] == 2 and candidate["usage"]["requests"] == candidate["usage"]["tool_calls"] == 2
    assert candidate["checkpoint_record_digest"] == before["journal"][-2]["digest"]
    assert candidate["response_record_digest"] == pending["digest"]
    action = uuid4().hex
    receipt = await c.service.checkpoints.request(c.run_id, failed["id"], action, checkpoint_input(candidate))
    queued = await native_checkpoint_cut(c, failed["id"])
    assert receipt["status"] == "queued" and receipt["action_id"] == action
    assert queued["count"] == 1 and queued["native"][:-1] == before["native"][:-1]
    assert queued["journal"][:-1] == before["journal"] and queued["journal"][-1]["kind"] == HANDOFF
    assert queued["events"][:-1] == before["events"] and queued["events"][-1]["kind"] == ADMITTED
    assert queued["commands"] == before["commands"] == c.initial_commands
    model, turns = finish_checkpoint_transport(c, monkeypatch)
    with mock_model_requests(model):
        await c.journal.recover()
        await c.service.execute(failed["id"])
    final = await native_checkpoint_cut(c, failed["id"])
    current = await c.store.get_run(c.run_id)
    assert final["ticket"]["status"] == "completed", final["ticket"].get("result")
    assert final["count"] == 1 and final["native"][:-1] == before["native"][:-1]
    assert final["task"][:4] == before["task"][:4] and final["task"][3] == 2
    assert final["ticket"]["generation"] != failed["generation"]
    assert final["ticket"]["owner_pid"] == c.store._ownership_pid
    for key in [
        "request",
        "request_digest",
        "checkpoint",
        "checkpoint_digest",
        "binding",
        "admission_seal",
        "lease",
        "task_key",
    ]:
        assert final["ticket"][key] == failed[key]
    assert final["journal"][: len(before["journal"])] == before["journal"]
    assert final["events"][: len(before["events"])] == before["events"]
    consumes = [
        e
        for e in final["journal"]
        if e["kind"] == "agent_consume" and e["idempotency_key"] == pending["idempotency_key"]
    ]
    assert len(consumes) == 1 and consumes[0]["payload"]["version"] == 2
    assert consumes[0]["payload"]["source"]["record_digest"] == pending["digest"]
    assert len(turns) == 1 and len(c.posts) == 4
    for call in [WRITE_CALL, PENDING_CALL]:
        assert len([m for m in turns[0]["messages"] if m.get("role") == "tool" and m.get("tool_call_id") == call]) == 1
    assert c.checkpoint_writes == [("existing.py", "VALUE = 2\n")] and c.checkpoint_reads == ["existing.py"]
    assert (c.engine.workspaces / c.run_id / "existing.py").read_text() == "VALUE = 2\n"
    assert current["status"] == "paused" and current["attempt"] == 2
    assert current["usage"]["requests"] == 4
    assert current["repair_budget"] == original_run["repair_budget"] == 2
    assert current["implementation_rounds"] == original_run["implementation_rounds"] + 1 == current["attempt"] == 2
    assert current["sandbox_snapshot"] == original_run["sandbox_snapshot"]
    assert current["sandbox_snapshot"]["configuration"]["command_limit"] == 22
    checkpoint = next(e for e in reversed(final["journal"]) if e["kind"] == "agent_checkpoint")
    payload = CheckpointPayloadV2.model_validate(checkpoint["payload"])
    boundary = BoundaryCheckpoint.decode(payload.snapshot.encode(), payload.checkpoint_digest)
    terminal = [e for e in final["journal"] if e["kind"] == "terminal_completion_receipt"]
    assert len(terminal) == 1
    assert terminal[0]["payload"]["proof"]["completions"][-1]["checkpoint_record_digest"] == checkpoint["digest"]
    assert boundary.record.limits == candidate["limits"]
    assert boundary.usage.requests == 3
    assert boundary.usage.tool_calls == 3
    assert len(final["commands"]) == len(c.command_calls) == 11 and final["commands"] == c.initial_commands
    await c.service.execute(failed["id"])
    assert await native_checkpoint_cut(c, failed["id"]) == final
    assert len(c.posts) == 4 and len(c.checkpoint_writes) == len(c.checkpoint_reads) == 1


async def test_native_checkpoint_rollback_and_forced_concurrent_same_action_are_atomic(native_continuity, monkeypatch):
    c = native_continuity
    failed = await seed_pending_checkpoint(c, monkeypatch)
    enable_checkpoint_continuation(c)
    candidate = await c.service.checkpoints.candidate(c.run_id, failed["id"])
    body, action = checkpoint_input(candidate), uuid4().hex
    before = await native_checkpoint_cut(c, failed["id"])
    event = c.requests._event

    async def rollback(row, kind, evidence):
        await event(row, kind, evidence)
        if kind == ADMITTED:
            raise RuntimeError("Fictional post-event transaction rollback")

    with monkeypatch.context() as patch:
        patch.setattr(c.requests, "_event", rollback)
        with pytest.raises(RuntimeError, match="rollback"):
            await c.service.checkpoints.request(c.run_id, failed["id"], action, body)
    assert await native_checkpoint_cut(c, failed["id"]) == before
    prepared = c.service.checkpoints._prepare
    barrier, ready = asyncio.Event(), 0

    async def together(*args):
        nonlocal ready
        value = await prepared(*args)
        ready += 1
        if ready == 3:
            barrier.set()
        await asyncio.wait_for(barrier.wait(), 20)
        return value

    with monkeypatch.context() as patch:
        patch.setattr(c.service.checkpoints, "_prepare", together)
        receipts = await asyncio.gather(
            *(c.service.checkpoints.request(c.run_id, failed["id"], action, body) for _ in range(3))
        )
    assert receipts[1:] == receipts[:1] * 2
    after = await native_checkpoint_cut(c, failed["id"])
    assert after["count"] == 1 and after["native"][:-1] == before["native"][:-1]
    assert after["journal"][:-1] == before["journal"] and after["journal"][-1]["kind"] == HANDOFF
    assert after["events"][:-1] == before["events"] and after["events"][-1]["kind"] == ADMITTED
    assert await c.service.checkpoints.request(c.run_id, failed["id"], action, body) == receipts[0]
    with pytest.raises(ValueError):
        await c.service.checkpoints.request(c.run_id, failed["id"], uuid4().hex, body)
    with pytest.raises(ValueError):
        await c.service.checkpoints.request(
            c.run_id, failed["id"], action, body.model_copy(update={"valid_for_seconds": 1})
        )
    assert await native_checkpoint_cut(c, failed["id"]) == after
    assert len(c.posts) == 3 and len(c.checkpoint_writes) == 1 and not c.checkpoint_reads


async def test_native_checkpoint_admission_cas_rejects_task_changed_after_preparation(native_continuity, monkeypatch):
    c = native_continuity
    failed = await seed_pending_checkpoint(c, monkeypatch)
    enable_checkpoint_continuation(c)
    candidate = await c.service.checkpoints.candidate(c.run_id, failed["id"])
    before = await native_checkpoint_cut(c, failed["id"])
    prepare = c.service.checkpoints._prepare

    async def changed(*args):
        value = await prepare(*args)
        async with c.store._unit():
            task = await c.journal._task(c.run_id, "implement")
            task.data = {**task.data, "fictional_native_cas_race": True}
            await c.journal.tasks.save(task)
        return value

    with monkeypatch.context() as patch:
        patch.setattr(c.service.checkpoints, "_prepare", changed)
        with pytest.raises(ValueError, match="changed"):
            await c.service.checkpoints.request(c.run_id, failed["id"], uuid4().hex, checkpoint_input(candidate))
    after = await native_checkpoint_cut(c, failed["id"])
    expected = copy.deepcopy(before)
    expected["task_data"]["fictional_native_cas_race"] = True
    assert after == expected
    async with c.store._unit():
        assert (await c.journal._task(c.run_id, "implement")).data["fictional_native_cas_race"] is True
    assert len(c.posts) == 3 and len(c.checkpoint_writes) == 1 and not c.checkpoint_reads


async def test_native_checkpoint_policy_revoked_before_claim_cannot_consume_or_replay(native_continuity, monkeypatch):
    c = native_continuity
    failed = await seed_pending_checkpoint(c, monkeypatch)
    enable_checkpoint_continuation(c)
    candidate = await c.service.checkpoints.candidate(c.run_id, failed["id"])
    await c.service.checkpoints.request(c.run_id, failed["id"], uuid4().hex, checkpoint_input(candidate))
    queued = await native_checkpoint_cut(c, failed["id"])
    write_policy(c.store.configuration, ContinuityLeasePolicyV1(version=1, enabled=False))
    await c.service.execute(failed["id"])
    after = await native_checkpoint_cut(c, failed["id"])
    assert after["ticket"]["status"] == "failed"
    assert after["ticket"]["generation"] == failed["generation"]
    assert after["ticket"]["owner_pid"] == failed["owner_pid"]
    assert after["journal"] == queued["journal"] and after["commands"] == queued["commands"]
    assert after["events"][:-1] == queued["events"] and after["events"][-1]["kind"] == "worker_failed"
    assert not any(e["kind"] == "worker_claim" for e in after["events"][len(queued["events"]) :])
    assert len(c.posts) == 3 and c.checkpoint_writes == [("existing.py", "VALUE = 2\n")] and not c.checkpoint_reads
