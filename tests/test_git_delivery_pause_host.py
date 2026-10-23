# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Optional checkpoint refusal must not consume a legitimate typed worker question."""

import asyncio
import copy
from types import SimpleNamespace

import pytest

from firefly_forge.engine import Engine, WorkerAttention
from tests.test_git_delivery_local import delivery_case as delivery_case


@pytest.fixture
def paused_delivery(delivery_case, monkeypatch):
    c = delivery_case()
    c.service.store.path = c.root.parent / "factory.db"
    engine = Engine(c.service.store, c.root.parent / "workspaces", lambda _: None, journal=c.service.journal)
    engine.git_deliveries = c.service
    c.state["run"]["worker_request"] = {}

    async def feedback(run_id, model):
        c.state["run"].update(
            status="paused",
            worker_request={**model.request.model_dump(mode="json"), "status": "pending", "attempt": 1},
        )

    async def locked(run_id):
        return SimpleNamespace(data=c.state["run"], status=c.state["run"]["status"])

    c.service.journal.worker_feedback = feedback
    c.service.journal._run = locked
    payload = {
        "id": "unit-question",
        "role_id": "implementer",
        "task_id": "implement",
        "attempt": 1,
        "disposition": "clarification",
        "summary": "A public decision is needed",
        "evidence_paths": [],
        "request": {
            "id": "public-question",
            "kind": "other",
            "question": "Use the next value?",
            "context": "The public unit awaits its owner.",
            "choices": [],
        },
    }
    c.invoke = lambda: engine._worker_feedback(
        "1" * 32, c.root, SimpleNamespace(worker_feedback=payload, usage={}), 1, copy.deepcopy
    )
    return c


async def test_checkpoint_refusal_keeps_typed_pause_and_distinct_durable_hold(paused_delivery):
    c = paused_delivery

    async def refused(*args, **kwargs):
        raise ValueError("Controlled refusal; no Git receipt")

    c.service.automatic = refused
    with pytest.raises(WorkerAttention, match="public-question"):
        await c.invoke()
    assert c.state["run"]["status"] == "paused" and c.state["run"]["worker_request"]["status"] == "pending"
    assert c.state["run"]["git_delivery_hold"]["question_id"] == "public-question"
    assert [e["kind"] for e in c.state["entries"]] == ["git_delivery_hold"]
    assert c.state["entries"][0]["payload"]["error_type"] == "ValueError"


@pytest.mark.parametrize("failure", [RuntimeError("Controlled lease loss"), asyncio.CancelledError()])
async def test_lease_loss_and_cancellation_are_never_relabelled_checkpoint_refusal(paused_delivery, failure):
    c = paused_delivery

    async def failed(*args, **kwargs):
        if isinstance(failure, RuntimeError):
            c.state["owned"] = False
        raise failure

    c.service.automatic = failed
    with pytest.raises(type(failure)):
        await c.invoke()
    assert not c.state["entries"] and "git_delivery_hold" not in c.state["run"]


async def test_late_checkpoint_refusal_cannot_overwrite_an_answered_question(paused_delivery):
    c = paused_delivery

    async def answered(*args, **kwargs):
        c.state["run"]["worker_request"]["status"] = "answered"
        raise ValueError("Controlled refusal after an operator answer")

    c.service.automatic = answered
    with pytest.raises(ValueError, match="changed"):
        await c.invoke()
    assert c.state["run"]["worker_request"]["status"] == "answered" and not c.state["entries"]
