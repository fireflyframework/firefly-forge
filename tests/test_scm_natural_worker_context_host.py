# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Legacy worker history cannot grant question authority or poison current status."""

import copy
import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

from firefly_forge.agent_roles import WorkerFeedback, WorkerRequest
from firefly_forge.journal import digest
from firefly_forge.scm_natural_models import NaturalDecision, resolve_task
from firefly_forge.scm_natural_tasks import SCMNaturalTasks


async def fixture():
    scope = dict(repository_id="a" * 32, project_id="b" * 32, native_user_id="c" * 32, binding_digest="d" * 64)
    delivery = dict(
        id="e" * 32,
        provider="github",
        repository_id=scope["repository_id"],
        actor="7",
        issue={"kind": "issue", "number": 4},
    )
    rows, runs, entries = [], {}, {}
    for index, legacy in enumerate((None, "missing_body", "response_source"), 1):
        rid, receipt = str(index) * 32, str(index + 4) * 32
        question = WorkerRequest(
            id=f"question-{index}",
            kind="other",
            question=f"Public question {index}?",
            context="Public bounded context",
            choices=[],
        )
        feedback = WorkerFeedback(
            id=f"feedback-{index}",
            role_id="qa",
            task_id="verify",
            attempt=1,
            disposition="clarification",
            summary="Waiting for a human answer",
            request=question,
        )
        payload = feedback.model_dump()
        request = {
            **question.model_dump(),
            "status": "pending" if legacy is None else "answered",
            "role_id": "qa",
            "task_id": "verify",
            "attempt": 1,
        }
        if legacy == "response_source":
            request["response_source"] = "legacy-native-human"
        runs[rid] = dict(id=rid, status="paused" if legacy is None else "failed", worker_request=request)
        event = dict(kind="worker_feedback", payload=payload, digest=digest(payload))
        entries[rid] = [SimpleNamespace(data=event)]
        body = f"Published public question {index}."
        intent = {"request_id": question.id, "body": body, "body_sha256": hashlib.sha256(body.encode()).hexdigest()}
        if legacy is not None:
            intent.pop("body")
        rows.append(
            SimpleNamespace(
                data={
                    **delivery,
                    "id": receipt,
                    "status": "processed",
                    "action_status": "completed",
                    "feedback_status": "sent",
                    "feedback_remote": {"id": index},
                    "feedback_intent": intent,
                }
            )
        )

    @asynccontextmanager
    async def unit():
        yield

    async def context(_):
        return copy.deepcopy(rows)

    async def get_run(key):
        return copy.deepcopy(runs[key])

    async def records(key, request_id):
        return copy.deepcopy(entries[key.hex])

    async def references(*args, **kwargs):
        return (
            [dict(run_id=rid, receipt_id=rows[index].data["id"]) for index, rid in enumerate(runs)],
            copy.deepcopy(runs),
        )

    store = SimpleNamespace(_unit=unit, get_run=get_run)
    collaboration = SimpleNamespace(
        store=store,
        inbox=SimpleNamespace(conversation_context=context),
        deployments=SimpleNamespace(
            engine=SimpleNamespace(journal=SimpleNamespace(records=SimpleNamespace(find_question_context=records)))
        ),
    )
    natural = SCMNaturalTasks(collaboration)
    natural.conversations.references = references
    return natural, delivery, [{**scope, "component_ids": ["api"]}], rows, runs


async def test_latest_qualified_question_survives_foreign_legacy_publication_and_answer_metadata():
    natural, delivery, scopes, _, _ = await fixture()
    context = await natural._context(delivery, scopes)
    assert len(context["candidates"]) == 3
    current, *legacy = context["candidates"]
    assert current["question_id"] == "question-1" and current["origin"] == "worker_journal"
    assert current["publication"]["receipt_id"] == "5" * 32
    assert all(row["question_id"] is None and row.get("publication") is None for row in legacy)
    assert all(row["question"] is None and row["choices"] == [] for row in legacy)
    assert context["status_context"]["selected_run_id"] == "1" * 32


async def test_unpublished_current_question_never_becomes_answerable_from_journal_alone():
    natural, delivery, scopes, rows, _ = await fixture()
    rows[0].data["feedback_intent"].pop("body")
    context = await natural._context(delivery, scopes)
    assert all(row["question_id"] is None and row["question"] is None for row in context["candidates"])


async def test_current_published_pending_question_tamper_still_refuses():
    natural, delivery, scopes, _, runs = await fixture()
    before = await natural._context(delivery, scopes)
    decision = NaturalDecision(intent="answer", message="I retained your answer.")
    selected = resolve_task(before["scope"], before["candidates"], decision, answer="Use the public option")
    assert selected.state == "answer"
    runs["1" * 32]["worker_request"]["question"] = "Changed outside its immutable typed producer"
    after = await natural._context(delivery, scopes)
    assert after != before
    held = resolve_task(after["scope"], after["candidates"], decision, answer="Use the public option")
    assert held.state == "waiting" and held.question_id is None
