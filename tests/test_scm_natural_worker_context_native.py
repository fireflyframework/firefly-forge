# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual DI/PG publication boundaries over current and legacy worker journals."""

import copy
import json
from uuid import UUID

import pytest
from scm_polling_native_fixtures import native_poll as native_poll
from test_addressed_status_native import natural
from worker_reply_fixtures import authenticated_operator

from firefly_forge.agent_roles import WorkerFeedback, WorkerRequest
from firefly_forge.model_profiles import ModelProfileInput
from firefly_forge.models import RunInput
from firefly_forge.scm_natural_models import NaturalDecision
from firefly_forge.task_routing import TaskRoutingInput


@pytest.mark.parametrize("tamper", [False, True])
def test_native_current_worker_question_survives_legacy_history_and_holds_after_tamper(
    native_poll, monkeypatch, tamper
):
    case = native_poll
    case.ready()
    case.consent()
    legacy = []

    async def older(index):
        async with authenticated_operator(case.store):
            run = await case.store.create_run(
                RunInput(
                    repository_id=case.repo["id"],
                    project_id=case.project["id"],
                    component_id="api",
                    operation="test",
                    agent="bridge",
                    frontend="none",
                    brief="Public historical status",
                )
            )
            await case.journal.ensure_plan(run["id"])
            await case.journal.worker_feedback(
                run["id"],
                WorkerFeedback(
                    id=f"legacy-feedback-{index}",
                    role_id="qa",
                    task_id="verify",
                    attempt=1,
                    disposition="clarification",
                    summary="An old typed question",
                    request=WorkerRequest(
                        id=f"legacy-question-{index}",
                        kind="other",
                        question="Historical public question?",
                        context="Historical context only",
                    ),
                ),
            )
            return await case.store.get_run(run["id"])

    for index in (1, 2):
        run = case.http.portal.call(older, index)
        source = natural(case, "/forge status " + json.dumps({"run_id": run["id"]}), number=80 + index)
        linked = case.process(source)
        assert linked["feedback_status"] == "sent"

        async def old_shape(run, linked, index):
            async with case.store._unit():
                row = await case.collaboration.inbox.find_by_id(UUID(linked["id"]))
                intent = copy.deepcopy(row.data["feedback_intent"])
                intent.pop("body")
                await case.collaboration._write_row(row, feedback_intent=intent)
            async with authenticated_operator(case.store):
                request = copy.deepcopy(run["worker_request"])
                request["status"] = "answered"
                if index == 2:
                    request["response_source"] = "legacy-native-human"
                await case.store.update(run["id"], status="failed", worker_request=request)

        case.http.portal.call(old_shape, run, linked, index)
        legacy.append(run["id"])
    linked = case.process(case.observe("status"))
    assert linked["feedback_status"] == "sent"
    before = case.http.portal.call(case.journal.entries, case.run["id"])
    posts = len(case.state["posts"])
    router = case.store.task_router
    router.profiles.save(
        "worker-context",
        ModelProfileInput(
            name="Controlled worker status",
            provider="agentic",
            values={"model": "openai-responses:public-worker", "api_key": "owned-worker-context-key"},
        ),
    )
    case.http.portal.call(router.save, TaskRoutingInput(bindings={"communication": "worker-context"}))
    contexts = []

    async def classify(message, context, settings):
        contexts.append(copy.deepcopy(context))
        if tamper:
            run = await case.store.get_run(case.run["id"])
            request = {**run["worker_request"], "question": "Changed outside the immutable producer"}
            async with authenticated_operator(case.store):
                await case.store.update(case.run["id"], worker_request=request)
        return NaturalDecision(
            intent="answer" if tamper else "status", message="The recorded question is awaiting your response."
        )

    monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
    delivery = natural(case, "@forge, how is this going?", number=90)
    result = case.process(delivery)
    assert len(contexts) == 1
    current = next(row for row in contexts[0]["candidates"] if row.get("run_id") == case.run["id"])
    assert current["question_id"] == "poll-native-question" and current["publication"]["receipt_id"] == linked["id"]
    old = [row for row in contexts[0]["candidates"] if row.get("run_id") in legacy]
    assert len(old) == 2 and all(row["question_id"] is None and row["question"] is None for row in old)
    if tamper:
        assert result["status"] == "needs_reconciliation" and result["feedback_status"] == "held"
        assert len(case.state["posts"]) == posts
        changed = case.http.portal.call(case.store.get_run, case.run["id"])
        assert changed["worker_request"]["question"] == "Changed outside the immutable producer"
    else:
        assert result["status"] == "processed" and result["feedback_status"] == "sent"
        assert result["run_id"] == case.run["id"] and len(case.state["posts"]) == posts + 1
    assert case.process(delivery) == result and len(contexts) == 1
    assert case.http.portal.call(case.journal.entries, case.run["id"]) == before
