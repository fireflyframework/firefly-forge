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
        "workspace": "/workspace",
        "cwd": ".",
    }


@pytest.mark.asyncio
async def test_paused_request_has_explicit_narrow_native_stage_admission(setup):
    memory, journal = setup
    expected = intent(memory)
    state = await journal.verification_begin(memory.id.hex, expected, check_current=lambda: None)
    assert memory.run.status == "paused"
    assert memory.task.status == "running"
    assert memory.task.attempt == 1
    assert memory.task.data["verification_handoff"] == expected["id"]
    assert state["status"] == "running"
    assert len(memory.rows) == 1
    await journal.verification_begin(memory.id.hex, expected, check_current=lambda: None)
    assert len(memory.rows) == 1


@pytest.mark.asyncio
async def test_changed_pending_request_refuses_before_stage_admission(setup):
    memory, journal = setup
    expected = intent(memory)
    memory.request["question"] = "changed"
    with pytest.raises(ValueError, match="request"):
        await journal.verification_begin(memory.id.hex, expected, check_current=lambda: None)
    assert memory.task.status == "paused"
    assert not memory.rows


@pytest.mark.asyncio
async def test_handoff_stage_refuses_commands_outside_its_frozen_slot(setup):
    memory, journal = setup
    expected = intent(memory)
    await journal.verification_begin(memory.id.hex, expected, check_current=lambda: None)
    payload = {
        "parent_attempt": 1,
        "ordinal": 2,
        "request": {"argv": ["delete"], "workspace": "/workspace", "cwd": "."},
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
    await journal.verification_begin(memory.id.hex, expected, check_current=lambda: None)
    receipts = []

    async def executions(*args):
        return receipts

    journal.sandbox_executions = executions
    with pytest.raises(ValueError, match="receipt"):
        await journal.verification_command(memory.id.hex, expected, 0, check_current=lambda: None)
    receipts.append(
        {
            "execution_id": uuid4().hex,
            "task_id": "implement",
            "parent_attempt": 1,
            "ordinal": 2,
            "effects": {"start": {"status": "done", "evidence": {"exit_code": 1, "output": "Actual missing frontend"}}},
        }
    )
    result = await journal.verification_command(memory.id.hex, expected, 0, check_current=lambda: None)
    assert result["failed_commands"] == 1
    state = await journal.verification_finish(memory.id.hex, expected, check_current=lambda: None)
    assert state["status"] == "completed"
    assert memory.task.status == "paused"
    assert memory.task.attempt == 1
    assert memory.run.data["worker_request"]["status"] == "answered"
    answer = memory.run.data["worker_response"]
    assert answer["source"] == "factory_verification"
    assert "1 failed" in answer["answer"]
    assert "Actual missing frontend" in answer["answer"]
    assert all(row.kind != "worker_response" for row in memory.rows)


@pytest.mark.asyncio
async def test_unknown_execution_holds_without_resolving_human_question(setup):
    memory, journal = setup
    expected = intent(memory)
    await journal.verification_begin(memory.id.hex, expected, check_current=lambda: None)
    await journal.verification_hold(memory.id.hex, expected["id"])
    assert memory.run.data["verification_handoff"]["status"] == "held"
    assert memory.run.data["worker_request"]["status"] == "pending"
    assert memory.task.status == "paused"
    with pytest.raises(ValueError):
        await journal.verification_begin(memory.id.hex, expected, check_current=lambda: None)


@pytest.mark.asyncio
async def test_current_cut_is_checked_after_native_authority_await(setup, monkeypatch):
    memory, journal = setup
    allowed = True

    async def revoke(*args):
        nonlocal allowed
        allowed = False

    def check():
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
        await journal.verification_begin(memory.id.hex, intent(memory), check_current=lambda: None)
    memory.request["kind"] = "verification"
    memory.task.attempt = 4
    memory.request["attempt"] = 4
    expected = intent(memory)
    expected["parent_attempt"] = 4
    with pytest.raises(ValueError):
        await journal.verification_begin(memory.id.hex, expected, check_current=lambda: None)
    assert memory.task.status == "paused"
