# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fictional pending response after one durable write in an isolated test database."""

import copy
import json
from functools import wraps
from uuid import uuid4

import httpx2
from test_agentic_checkpoint_activation_host import result
from test_bridge_completion_native import mock_model_requests
from test_invalid_read_continuity_native import consent

from firefly_forge.agent_checkpoints import AgentCheckpoints
from firefly_forge.checkpoint_continuation_models import CheckpointContinuationInputV1
from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, write_policy
from firefly_forge.store import identifier
from firefly_forge.workspace_tools import WorkspaceTools

WRITE_CALL = "fictional-known-write"
PENDING_CALL = "fictional-pending-read"


def enable_checkpoint_continuation(c):
    write_policy(
        c.store.configuration,
        ContinuityLeasePolicyV1(version=1, enabled=True, allow_checkpoint_continuation=True, max_window_seconds=180),
    )


def checkpoint_input(candidate):
    return CheckpointContinuationInputV1(
        version=1,
        candidate_digest=candidate["digest"],
        valid_for_seconds=180,
        acknowledge_retained_response_consumed_once=True,
        acknowledge_no_completed_effect_replay=True,
        acknowledge_same_request_attempt_and_budgets=True,
        acknowledge_finite_ownership_lease=True,
    )


async def native_checkpoint_cut(c, request_id):
    async with c.store._unit():
        row = await c.requests.records.find_by_id(identifier(request_id))
        assert row is not None
        count = await c.requests.records.count_for_run(identifier(c.run_id), "invalid_read")
        task = await c.journal._task(c.run_id, "implement")
        scalars = (row.id.hex, row.run_id.hex, row.kind, row.request_key, row.status)
        task_scalars = (task.id.hex, task.plan_id.hex, task.task_id, task.attempt, task.status)
        ticket = copy.deepcopy(row.data)
        task_data = copy.deepcopy(task.data)
        assert (row.id.hex, row.run_id.hex, row.kind, row.status) == (
            ticket["id"],
            ticket["run_id"],
            ticket["kind"],
            ticket["status"],
        )
        native_events = await c.requests.journal.events(identifier(request_id))
        assert all(
            event.request_id.hex == event.data["request_id"] and event.sequence == event.data["sequence"]
            for event in native_events
        )
        events = [copy.deepcopy(event.data) for event in native_events]
        assert c.requests.verify_chain(events)
    return {
        "count": count,
        "native": scalars,
        "task": task_scalars,
        "task_data": task_data,
        "ticket": ticket,
        "events": events,
        "journal": copy.deepcopy(await c.journal.entries(c.run_id)),
        "commands": copy.deepcopy(await c.journal.sandbox_executions(c.run_id)),
    }


async def seed_pending_checkpoint(c, monkeypatch):
    """Run real native custody with controlled model HTTP; interrupt before consuming turn three."""
    ticket = await c.service.request(c.run_id, uuid4().hex, await consent(c))
    model = c.engine.provider_factory("bridge").model()
    client = model.client._client
    original_transport = client._transport
    original_consume = AgentCheckpoints._consume
    original_write, original_read = WorkspaceTools.write_file, WorkspaceTools.read_file
    c.checkpoint_writes, c.checkpoint_reads = [], []

    @wraps(original_write)
    async def write(self, path: str, content: str, *, expected_sha256: str | None = None) -> str:
        c.checkpoint_writes.append((path, content))
        return await original_write(self, path, content, expected_sha256=expected_sha256)

    @wraps(original_read)
    async def read(self, path: str) -> str:
        c.checkpoint_reads.append(path)
        return await original_read(self, path)

    async def stop(self, run, intent, body, current, reader):
        if PENDING_CALL.encode() in body:
            raise ValueError("Fictional ownership expiry before retained response consumption")
        return await original_consume(self, run, intent, body, current, reader)

    def transport(request):
        if request.method == "GET":
            return original_transport.handler(request)
        c.posts.append(request.content)
        assert len(c.posts) in {2, 3}, "Seeding cannot redispatch an earlier completion"
        body = result(tool=True)
        call = body["choices"][0]["message"]["tool_calls"][0]
        call.update(
            id=WRITE_CALL if len(c.posts) == 2 else PENDING_CALL,
            function={
                "name": "write_file" if len(c.posts) == 2 else "read_file",
                "arguments": '{"path":"existing.py","content":"VALUE = 2\\n"}'
                if len(c.posts) == 2
                else '{"path":"existing.py"}',
            },
        )
        return httpx2.Response(200, json=body)

    monkeypatch.setattr(WorkspaceTools, "write_file", write)
    monkeypatch.setattr(WorkspaceTools, "read_file", read)
    try:
        client._transport = httpx2.MockTransport(transport)
        with monkeypatch.context() as stopped, mock_model_requests(model):
            stopped.setattr(AgentCheckpoints, "_consume", stop)
            await c.service.execute(ticket["id"])
    finally:
        client._transport = original_transport
    failed = await c.service.get(c.run_id, ticket["id"])
    prefix = await c.journal.entries(c.run_id)
    assert failed["status"] == "needs_reconciliation", failed.get("result")
    assert [e["kind"] for e in prefix[-3:]] == ["bridge_request_intent", "agent_checkpoint", "bridge_request_response"]
    assert len(c.posts) == 3 and c.checkpoint_writes == [("existing.py", "VALUE = 2\n")]
    assert c.checkpoint_reads == []
    assert (c.engine.workspaces / c.run_id / "existing.py").read_text() == "VALUE = 2\n"
    assert not any(
        e["kind"] == "agent_consume" and e["idempotency_key"] == prefix[-1]["idempotency_key"] for e in prefix
    )
    return failed


def finish_checkpoint_transport(c, monkeypatch):
    """Expose one new completion after the retained response; no old callback is replaced."""
    model = c.engine.provider_factory("bridge").model()
    client = model.client._client
    original = client._transport
    turns = []

    def transport(request):
        if request.method == "GET":
            return original.handler(request)
        turns.append(json.loads(request.content))
        c.posts.append(request.content)
        assert len(turns) == 1, "Only the next model turn is permitted"
        return httpx2.Response(
            200,
            json=result(
                json.dumps(
                    {
                        "summary": "Fictional retained checkpoint continued",
                        "qa_report": None,
                        "worker_feedback": {
                            "id": "fictional-checkpoint-review",
                            "role_id": "implementer",
                            "task_id": "implement",
                            "attempt": 2,
                            "disposition": "clarification",
                            "summary": "Completed write preserved",
                            "request": {
                                "id": "fictional-checkpoint-review",
                                "kind": "product_definition",
                                "question": "Proceed?",
                                "context": "Controlled native fixture",
                            },
                        },
                    }
                )
            ),
        )

    monkeypatch.setattr(client, "_transport", httpx2.MockTransport(transport))
    return model, turns
