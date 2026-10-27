# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PREPARED, UNRUN: one isolated native request, explicit recovery, no live provider.

Only the root database operator may execute these cases in a fresh _test schema.
The inherited historical producer and resumed model use public MockTransport.
"""

import asyncio
import copy
from uuid import uuid4

import pytest
from test_invalid_read_continuity_native import consent
from test_invalid_read_recovery_host import recovery_input

pytest_plugins = ["test_invalid_read_continuity_native"]


async def failed(c):
    request_id = uuid4().hex
    await c.service.request(c.run_id, request_id, await consent(c))
    await c.service._finish(request_id, "failed", {"detail": "Controlled failure before worker claim"})
    async with c.store._unit():
        task = await c.journal._task(c.run_id, "implement")
        task.status = "needs_reconciliation"
        await c.journal.tasks.save(task)
    # A real startup may append a typed reservation observation; both are retained.
    await c.journal.recover()
    return request_id


async def test_native_same_id_recovery_rolls_back_and_concurrent_actions_are_idempotent(native_continuity, monkeypatch):
    c = native_continuity
    request_id = await failed(c)
    service = c.service.recoveries
    candidate = await service.candidate(c.run_id, request_id)
    body = recovery_input(candidate)
    recovery_id = uuid4().hex
    original = copy.deepcopy(await c.service.get(c.run_id, request_id))
    entries = copy.deepcopy(await c.journal.entries(c.run_id))
    events = copy.deepcopy(await c.requests.events(request_id))
    event = c.requests._event

    async def reject(row, kind, evidence):
        result = await event(row, kind, evidence)
        if kind == "request_recovered_before_claim":
            raise RuntimeError("Controlled post-event rollback")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(c.requests, "_event", reject)
        with pytest.raises(RuntimeError, match="rollback"):
            await service.request(c.run_id, request_id, recovery_id, body)
    assert await c.service.get(c.run_id, request_id) == original
    assert await c.journal.entries(c.run_id) == entries
    assert await c.requests.events(request_id) == events
    replies = await asyncio.gather(*(service.request(c.run_id, request_id, recovery_id, body) for _ in range(3)))
    assert replies[1:] == replies[:1] * 2
    assert replies[0]["request_id"] == request_id and replies[0]["status"] == "queued"
    assert len([e for e in await c.journal.entries(c.run_id) if e["kind"] == "invalid_read_recovery"]) == 1
    assert len(await c.requests.events(request_id)) == 3 and len(c.posts) == 1


async def test_native_same_id_recovery_reaches_worker_and_charges_only_new_commands(native_continuity):
    c = native_continuity
    request_id = await failed(c)
    before = copy.deepcopy(await c.service.get(c.run_id, request_id))
    prefix = copy.deepcopy(await c.journal.entries(c.run_id))
    candidate = await c.service.recoveries.candidate(c.run_id, request_id)
    assert candidate["usage"]["commands"] == 11 and candidate["usage"]["command_limit"] == 22
    await c.service.recoveries.request(c.run_id, request_id, uuid4().hex, recovery_input(candidate))
    await c.service.execute(request_id)
    after = await c.service.get(c.run_id, request_id)
    assert after["status"] == "completed", after.get("result")
    assert after["admission_seal"] == before["admission_seal"] and after["checkpoint"] == before["checkpoint"]
    assert [e["kind"] for e in await c.requests.events(request_id)] == [
        "request_admitted",
        "worker_failed",
        "request_recovered_before_claim",
        "worker_claim",
        "worker_completed",
    ]
    assert (await c.journal.entries(c.run_id))[: len(prefix)] == prefix
    commands = await c.journal.sandbox_executions(c.run_id)
    assert len(commands) == 13 and commands[:11] == c.initial_commands
    assert len(c.posts) == 7
    await c.service.execute(request_id)
    assert len(c.posts) == 7 and len(await c.journal.sandbox_executions(c.run_id)) == 13
