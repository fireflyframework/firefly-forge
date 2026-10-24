# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_forge.journal import Journal, digest
from firefly_forge.project_access import ProjectAccess


class Memory:
    def __init__(self):
        self.id = uuid4()
        self.run = SimpleNamespace(id=self.id, status="paused", data={})
        self.request = {
            "id": "arbitrary-request",
            "kind": "verification",
            "status": "pending",
            "task_id": "implement",
            "attempt": 1,
        }
        self.run.data = {
            "id": self.id.hex,
            "status": "paused",
            "worker_request": self.request,
            "commands": [{"argv": ["test"], "timeout": 30}],
        }
        self.task = SimpleNamespace(task_id="implement", status="paused", attempt=1, data={})
        self.rows = []
        self.effects = []
        self.run_records = self
        self.ownership_checks = 0

    async def assert_ownership(self):
        self.ownership_checks += 1

    @asynccontextmanager
    async def _unit(self):
        yield self

    async def get_run(self, run_id):
        return copy.deepcopy(self.run.data)

    async def find_by_id(self, run_id, **kwargs):
        return self.run

    async def find_for_run(self, run_id):
        return self.rows

    async def save(self, row):
        if hasattr(row, "kind"):
            self.rows.append(row)


@pytest.fixture
def setup(monkeypatch):
    memory = Memory()
    journal = Journal(memory, records=memory)
    journal.tasks = SimpleNamespace(save=memory.save)

    async def task(*args):
        return memory.task

    async def authority(*args):
        return None

    journal._task = task
    monkeypatch.setattr(ProjectAccess, "authorize_execution", authority)
    return memory, journal


def intent(memory):
    return {
        "version": 1,
        "id": uuid4().hex,
        "request_id": memory.request["id"],
        "request_digest": digest(memory.request),
        "source_digest": "a" * 64,
        "commands_digest": digest({"commands": memory.run.data["commands"]}),
        "policy_digest": "b" * 64,
        "provider_digest": "c" * 64,
        "continuation": 1,
        "max_continuations": 1,
        "commands_count": 1,
        "parent_attempt": 1,
        "ordinals": [2],
        "timeouts": [30],
        "sandbox_digest": "a" * 64,
        "workspace": "/workspace",
        "cwd": ".",
    }


@pytest.mark.asyncio
async def test_paused_request_has_explicit_narrow_native_stage_admission(setup):
    memory, journal = setup
    expected = intent(memory)
    state = await journal.verification_begin(memory.id.hex, expected, check_current=lambda current: None)
    assert memory.run.status == "paused"
    assert memory.ownership_checks == 1
    assert memory.task.status == "running"
    assert memory.task.attempt == 1
    assert memory.task.data["verification_handoff"] == expected["id"]
    assert state["status"] == "running"
    assert len(memory.rows) == 1
    await journal.verification_begin(memory.id.hex, expected, check_current=lambda current: None)
    assert len(memory.rows) == 1


@pytest.mark.asyncio
async def test_changed_pending_request_refuses_before_stage_admission(setup):
    memory, journal = setup
    expected = intent(memory)
    memory.request["question"] = "changed"
    with pytest.raises(ValueError, match="request"):
        await journal.verification_begin(memory.id.hex, expected, check_current=lambda current: None)
    assert memory.task.status == "paused"
    assert not memory.rows


@pytest.mark.asyncio
async def test_handoff_stage_refuses_commands_outside_its_frozen_slot(setup):
    memory, journal = setup
    expected = intent(memory)
    await journal.verification_begin(memory.id.hex, expected, check_current=lambda current: None)
    payload = {
        "parent_attempt": 1,
        "ordinal": 2,
        "snapshot_digest": "a" * 64,
        "request": {"argv": ["delete"], "workspace": "/workspace", "cwd": ".", "timeout": 30},
    }
    with pytest.raises(ValueError, match="frozen verification"):
        await journal.verification_effect_authority(memory.id.hex, "implement", payload)
    payload["request"]["argv"] = ["test"]
    await journal.verification_effect_authority(memory.id.hex, "implement", payload)
    payload["ordinal"] = 1
    with pytest.raises(ValueError, match="slot"):
        await journal.verification_effect_authority(memory.id.hex, "implement", payload)
    assert len(memory.rows) == 1


@pytest.mark.asyncio
async def test_completion_requires_actual_native_receipt_and_distinguishes_system_answer(setup):
    memory, journal = setup
    expected = intent(memory)
    await journal.verification_begin(memory.id.hex, expected, check_current=lambda current: None)
    receipts = []

    async def executions(*args):
        return receipts

    journal.sandbox_executions = executions
    with pytest.raises(ValueError, match="receipt"):
        await journal.verification_command(memory.id.hex, expected, 0, check_current=lambda current: None)
    receipts.append(
        {
            "execution_id": uuid4().hex,
            "task_id": "implement",
            "parent_attempt": 1,
            "ordinal": 2,
            "effects": {"start": {"status": "done", "evidence": {"exit_code": 1, "output": "Actual missing frontend"}}},
        }
    )
    result = await journal.verification_command(memory.id.hex, expected, 0, check_current=lambda current: None)
    assert result["failed_commands"] == 1
    state = await journal.verification_finish(memory.id.hex, expected, check_current=lambda current: None)
    assert state["status"] == "completed"
    assert memory.task.status == "paused"
    assert memory.task.attempt == 1
    assert memory.run.data["worker_request"]["status"] == "answered"
    answer = memory.run.data["worker_response"]
    assert answer["source"] == "factory_verification"
    assert "1 failed" in answer["answer"]
    assert "Actual missing frontend" in answer["answer"]
    response = next(row.data["payload"] for row in memory.rows if row.kind == "worker_response")
    assert response["responding_actor"] == {
        "version": 4,
        "authentication_method": "factory_verification",
        "handoff_id": expected["id"],
        **{key: expected[key] for key in ("source_digest", "commands_digest", "policy_digest", "provider_digest")},
    }


@pytest.mark.asyncio
async def test_unknown_execution_holds_without_resolving_human_question(setup):
    memory, journal = setup
    expected = intent(memory)
    await journal.verification_begin(memory.id.hex, expected, check_current=lambda current: None)
    await journal.verification_hold(memory.id.hex, expected["id"])
    assert memory.run.data["verification_handoff"]["status"] == "held"
    assert memory.run.data["worker_request"]["status"] == "pending"
    assert memory.task.status == "paused"
    with pytest.raises(ValueError):
        await journal.verification_begin(memory.id.hex, expected, check_current=lambda current: None)


@pytest.mark.asyncio
async def test_only_completed_resumed_native_receipt_grants_one_extra_model_turn(setup):
    memory, journal = setup
    handoff_id = uuid4().hex
    memory.rows = [
        SimpleNamespace(
            kind="verification_handoff_intent",
            data={"payload": {"id": handoff_id, "continuation": 1, "max_continuations": 1}},
        ),
        SimpleNamespace(
            kind="verification_handoff_outcome", data={"payload": {"id": handoff_id, "status": "completed"}}
        ),
        SimpleNamespace(
            kind="worker_response",
            data={
                "payload": {
                    "responding_actor": {
                        "version": 4,
                        "authentication_method": "factory_verification",
                        "handoff_id": handoff_id,
                    }
                }
            },
        ),
    ]
    assert await journal.verification_continuation_allowance(memory.id.hex) == 0
    memory.rows.append(
        SimpleNamespace(kind="resume", data={"payload": {"automatic": True, "verification_handoff_id": handoff_id}})
    )
    assert await journal.verification_continuation_allowance(memory.id.hex) == 1
    memory.rows.append(memory.rows[-1])
    assert await journal.verification_continuation_allowance(memory.id.hex) == 1
    memory.rows[1].data["payload"]["status"] = "held"
    assert await journal.verification_continuation_allowance(memory.id.hex) == 0


@pytest.mark.asyncio
async def test_legacy_adoption_requires_native_actor_and_exact_original_body(setup, monkeypatch):
    from firefly_forge.identity import Principal
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository
    from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied

    memory, journal = setup
    body = {
        "request_id": uuid4().hex,
        "agent": "agentic",
        "pending_request_digest": digest(memory.request),
        **{key: "a" * 64 for key in ("source_digest", "commands_digest", "policy_digest", "provider_digest")},
    }
    with pytest.raises(AccessDenied):
        await journal.verification_adopt(memory.id.hex, body, {}, check_current=lambda current: None)
    actor = Principal(uuid4(), "fixture", "native")

    async def identity(*args, **kwargs):
        return SimpleNamespace(id=actor.id, enabled=True, issuer=actor.issuer, subject=actor.subject)

    async def authority(*args, **kwargs):
        pass

    monkeypatch.setattr(ForgeIdentityUserRepository, "find_by_id", identity)
    monkeypatch.setattr(journal, "_worker_authority", authority)
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        first = await journal.verification_adopt(memory.id.hex, body, {}, check_current=lambda current: None)
        assert first["request"] == body
        assert len(memory.rows) == 1
        same = await journal.verification_adopt(memory.id.hex, body, {}, check_current=lambda current: None)
        assert same == first and len(memory.rows) == 1
        with pytest.raises(ValueError, match="different"):
            await journal.verification_adopt(
                memory.id.hex, {**body, "agent": "codex"}, {}, check_current=lambda current: None
            )
    finally:
        CURRENT_PRINCIPAL.reset(token)


@pytest.mark.asyncio
async def test_current_cut_is_checked_after_native_authority_await(setup, monkeypatch):
    memory, journal = setup
    allowed = True

    async def revoke(*args):
        nonlocal allowed
        allowed = False

    def check(current):
        if not allowed:
            raise ValueError("Current policy changed")

    monkeypatch.setattr(ProjectAccess, "authorize_execution", revoke)
    with pytest.raises(ValueError, match="policy changed"):
        await journal.verification_begin(memory.id.hex, intent(memory), check_current=check)
    assert memory.task.status == "paused"
    assert not memory.rows


@pytest.mark.asyncio
async def test_unrelated_kind_and_exhausted_attempt_are_manual(setup):
    memory, journal = setup
    memory.request["kind"] = "approval"
    with pytest.raises(ValueError):
        await journal.verification_begin(memory.id.hex, intent(memory), check_current=lambda current: None)
    memory.request["kind"] = "verification"
    memory.task.attempt = 4
    memory.request["attempt"] = 4
    expected = intent(memory)
    expected["parent_attempt"] = 4
    with pytest.raises(ValueError):
        await journal.verification_begin(memory.id.hex, expected, check_current=lambda current: None)
    assert memory.task.status == "paused"
