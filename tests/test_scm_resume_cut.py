# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Causal resume eligibility using actual native journal envelopes, without a database."""

import copy
import importlib
import importlib.util
from types import SimpleNamespace

import pytest

from firefly_forge.agent_roles import WorkerFeedback, WorkerRequest, WorkerResponse
from firefly_forge.journal import Journal, digest
from firefly_forge.models import RunInput

RUN, REPO, PROJECT, USER = (x * 32 for x in "1234")
BINDING = "5" * 64
GRANT = "6" * 64


def eligibility():
    assert importlib.util.find_spec("firefly_forge.scm_resume_cut") is not None, (
        "Causal SCM resume eligibility is missing"
    )
    return importlib.import_module("firefly_forge.scm_resume_cut").resume_cut


@pytest.fixture
async def answered_cut():
    rows = []

    async def find(run_id):
        assert run_id.hex == RUN
        return rows

    async def save(row):
        rows.append(row)

    journal = Journal(SimpleNamespace(), records=SimpleNamespace(find_for_run=find, save=save))
    inputs = RunInput(
        repository_id=REPO,
        project_id=PROJECT,
        component_id="api",
        brief="Verify an existing component",
        agent="bridge",
        frontend="none",
    ).model_dump(mode="json")
    feedback = WorkerFeedback(
        id="feedback-1",
        role_id="implementer",
        task_id="implement",
        attempt=1,
        disposition="clarification",
        summary="An explicit answer is required",
        request=WorkerRequest(
            id="question-1",
            kind="product_definition",
            question="Which result?",
            context="The existing result is ambiguous",
        ),
    ).model_dump(mode="json")
    response = {
        **WorkerResponse(id="answer-1", actor="7", answer="Use the empty result").model_dump(mode="json"),
        "request_id": "question-1",
        "responding_actor": {"version": 1, "user_id": USER, "authentication_method": "oidc_session"},
    }
    plan = dict(
        version=1,
        revision=1,
        run_id=RUN,
        repository_id=REPO,
        project_id=PROJECT,
        component_id="api",
        agent="bridge",
        tasks=[dict(id="implement", kind="local")],
    )
    plan["digest"] = digest(plan)
    await journal._append(RUN, "plan", "plan-1", "plan", {"revision": 1, "digest": plan["digest"]})
    await journal._append(RUN, "implement", "worker-feedback:feedback-1", "worker_feedback", feedback)
    await journal._append(RUN, "implement", "worker-response:answer-1", "worker_response", response)
    run = {
        **inputs,
        "id": RUN,
        "status": "paused",
        "execution_actor": {"user_id": USER},
        "repair_budget": 2,
        "retry_count": 0,
        "config_revision": "7" * 32,
        "worker_feedback": feedback,
        "worker_response": response,
        "worker_request": {
            **feedback["request"],
            "status": "answered",
            "role_id": "implementer",
            "task_id": "implement",
            "attempt": 1,
        },
        "pause": {"kind": "worker_request", "automatic": False},
    }
    tasks = [dict(task_id="implement", status="paused", attempt=1, data={"known_worker_response": True})]
    return dict(
        run=run,
        plan=plan,
        tasks=tasks,
        entries=[copy.deepcopy(row.data) for row in rows],
        grant_digest=GRANT,
        binding_digest=BINDING,
        native_user_id=USER,
    )


async def test_answered_clarification_cut_identifies_exact_wal_and_frozen_scope(answered_cut):
    cut = eligibility()(**answered_cut)
    assert cut["version"] == 2 and cut["run_id"] == RUN and cut["project_id"] == PROJECT
    assert cut["question_id"] == "question-1" and cut["feedback_sequence"] == 2 and cut["response_sequence"] == 3
    assert cut["response_digest"] == answered_cut["entries"][2]["digest"]
    assert cut["grant_digest"] == GRANT and cut["binding_digest"] == BINDING
    assert cut["digest"] == digest({k: v for k, v in cut.items() if k != "digest"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "needs_reconciliation"),
        ("status", "interrupted"),
        ("status", "queued"),
        ("publish", True),
        ("cancel_request", {"status": "requested"}),
        ("publication_status", "publishing"),
        ("execution_actor", None),
    ],
)
async def test_unknown_cancelled_or_unowned_work_never_becomes_resume_eligible(answered_cut, field, value):
    answered_cut["run"][field] = value
    with pytest.raises(ValueError):
        eligibility()(**answered_cut)


async def test_mutable_answer_or_task_row_cannot_replace_immutable_causality(answered_cut):
    check = eligibility()
    original = copy.deepcopy(answered_cut)
    for mutation in ("answer", "question", "attempt", "known", "actor"):
        candidate = copy.deepcopy(original)
        if mutation == "answer":
            candidate["run"]["worker_response"]["answer"] = "A different response"
        elif mutation == "question":
            candidate["run"]["worker_request"]["id"] = "later-question"
        elif mutation == "attempt":
            candidate["tasks"][0]["attempt"] = 2
        elif mutation == "known":
            candidate["tasks"][0]["data"]["known_worker_response"] = False
        else:
            candidate["native_user_id"] = "9" * 32
        with pytest.raises(ValueError):
            check(**candidate)


async def test_legacy_or_foreign_answer_attribution_cannot_grant_resume(answered_cut):
    candidate = copy.deepcopy(answered_cut)
    response = candidate["entries"][-1]
    del response["payload"]["responding_actor"]
    response["digest"] = digest({k: v for k, v in response.items() if k != "digest"})
    candidate["run"]["worker_response"] = copy.deepcopy(response["payload"])
    with pytest.raises(ValueError):
        eligibility()(**candidate)


async def test_open_remote_effect_or_mixed_run_journal_refuses_instead_of_replaying(answered_cut):
    candidate = copy.deepcopy(answered_cut)
    candidate["tasks"].append(dict(task_id="sandbox:command", status="needs_reconciliation", attempt=1, data={}))
    with pytest.raises(ValueError):
        eligibility()(**candidate)
    candidate = copy.deepcopy(answered_cut)
    candidate["entries"][-1]["run_id"] = "9" * 32
    candidate["entries"][-1]["digest"] = digest({k: v for k, v in candidate["entries"][-1].items() if k != "digest"})
    with pytest.raises(ValueError):
        eligibility()(**candidate)


async def test_model_source_budget_or_campaign_changes_produce_different_review_cut(answered_cut):
    original = eligibility()(**answered_cut)
    for field, value in [
        ("model_profile_id", "another-model"),
        ("config_revision", "8" * 32),
        ("base_revision", "a" * 40),
        ("repair_budget", 3),
        ("retry_count", 1),
        ("campaign_context", {"campaign_id": "9" * 32}),
    ]:
        changed = copy.deepcopy(answered_cut)
        changed["run"][field] = value
        assert eligibility()(**changed)["digest"] != original["digest"]


async def test_any_wal_head_advance_invalidates_the_reviewed_cut(answered_cut):
    original = eligibility()(**answered_cut)
    rows = [
        SimpleNamespace(
            data=copy.deepcopy(item), digest=item["digest"], idempotency_key=item["idempotency_key"], kind=item["kind"]
        )
        for item in answered_cut["entries"]
    ]

    async def find(_):
        return rows

    async def save(row):
        rows.append(row)

    journal = Journal(SimpleNamespace(), records=SimpleNamespace(find_for_run=find, save=save))
    await journal._append(RUN, "implement", "note-1", "decision_noted", {"note": "Another observed decision"})
    answered_cut["entries"] = [copy.deepcopy(row.data) for row in rows]
    assert eligibility()(**answered_cut)["digest"] != original["digest"]


@pytest.mark.parametrize("field,value", [("agent", "claude"), ("repair_budget", 3)])
async def test_a_fresh_review_cannot_approve_run_fields_that_contradict_the_original_plan(answered_cut, field, value):
    # Bind an original plan field in a genuine journal envelope, then alter only the mutable run.
    answered_cut["plan"][field] = answered_cut["run"][field]
    answered_cut["plan"]["digest"] = digest({k: v for k, v in answered_cut["plan"].items() if k != "digest"})
    previous = "0" * 64
    for item in answered_cut["entries"]:
        if item["kind"] == "plan":
            item["payload"]["digest"] = answered_cut["plan"]["digest"]
        item["previous_hash"] = previous
        item["digest"] = digest({k: v for k, v in item.items() if k != "digest"})
        previous = item["digest"]
    answered_cut["run"][field] = value
    with pytest.raises(ValueError):
        eligibility()(**answered_cut)


@pytest.mark.parametrize(
    "field,value",
    [("publication_status", "needs_reconciliation"), ("cancel_request", {"status": "needs_reconciliation"})],
)
async def test_ambiguous_cancel_or_publication_state_never_produces_a_resume_cut(answered_cut, field, value):
    answered_cut["run"][field] = value
    with pytest.raises(ValueError):
        eligibility()(**answered_cut)
