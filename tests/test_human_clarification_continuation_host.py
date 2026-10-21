# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real Engine/Journal with memory rows; native identity and PostgreSQL are separate gates."""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_bridge_inflight_cancel import RUN, MemoryStore, Rows

from firefly_forge.agent_roles import RoleRegistry, WorkerFeedback, WorkerResponse
from firefly_forge.configuration import ConfigurationStore
from firefly_forge.engine import Engine
from firefly_forge.identity import Principal
from firefly_forge.journal import Journal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.providers import AgentResult, ProviderError


@pytest.fixture
def clarification_setup(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memory")
    store.configuration = ConfigurationStore(tmp_path / "settings")
    store.run.repository_id = UUID(store.run.data["repository_id"])
    store.run.data.update(id=RUN, role_snapshot=RoleRegistry(tmp_path / "roles").snapshot())
    store.reject_credentials = lambda value: None
    store.identity_enabled = True

    async def campaign(*args, **kwargs):
        pass

    async def administrator(self, principal):
        return principal is not None and principal.id == actor.id

    async def identity(self, identifier, **kwargs):
        return SimpleNamespace(id=actor.id, enabled=store.identity_enabled, issuer=actor.issuer, subject=actor.subject)

    store.assert_campaign_current = campaign
    actor = Principal(
        uuid4(), "https://fixture.invalid", "human-clarification", authentication_method="local_bootstrap"
    )
    monkeypatch.setattr(ProjectAccess, "current_administrator", administrator)
    monkeypatch.setattr("firefly_forge.identity_persistence.ForgeIdentityUserRepository.find_by_id", identity)
    plans, tasks, records = Rows(), Rows(), Rows()
    journal = Journal(store, plans=plans, tasks=tasks, records=records)
    return store, journal, actor


@pytest.mark.parametrize("restart", [False, True])
async def test_zero_repair_question_waits_for_answer_and_explicit_resume_before_second_provider(
    clarification_setup, tmp_path, monkeypatch, restart
):
    store, journal, actor = clarification_setup
    prompts = []

    class Worker:
        async def execute(self, workspace, brief, *args):
            prompts.append(brief)
            if len(prompts) == 2:
                raise ProviderError("Controlled stop after the second provider boundary")
            return AgentResult(
                "A human product decision is required",
                "fixture",
                usage={
                    "worker_feedback": {
                        "id": "feedback-owner",
                        "role_id": "implementer",
                        "task_id": "implement",
                        "attempt": 1,
                        "disposition": "clarification",
                        "summary": "Missing owner",
                        "request": {
                            "id": "owner",
                            "kind": "product_definition",
                            "question": "Which owner?",
                            "context": "The product brief has no owner",
                        },
                    }
                },
            )

    engine = Engine(store, tmp_path / "workspaces", lambda _: Worker(), journal=journal)
    workspace = engine.workspaces / RUN
    workspace.mkdir()
    (workspace / "service.py").write_text("VALUE = 1\n")

    async def inspect(*args):
        return {"base_revision": "b" * 40, "head": "b" * 40, "source_tree": "c" * 40}

    async def checked(argv, *args, **kwargs):
        assert argv == ["git", "ls-files", "-z"]
        return ""

    monkeypatch.setattr(engine, "_inspect_workspace", inspect)
    monkeypatch.setattr("firefly_forge.engine.checked", checked)
    await engine.execute_run(RUN)
    assert len(prompts) == 1 and store.run.data["status"] == "paused", store.run.data
    assert store.run.data["implementation_rounds"] == 1 and store.run.data["repair_budget"] == 0
    with pytest.raises(ValueError, match="Answer"):
        await journal.resume(RUN)
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        response = WorkerResponse(id="answer-owner", actor="Fictional operator", answer="Support owns this product")
        await journal.worker_response(RUN, "owner", response)
        assert store.run.data["status"] == "paused" and len(prompts) == 1
        if restart:
            journal = Journal(store, plans=journal.plans, tasks=journal.tasks, records=journal.records)
            engine.journal = journal
        await journal.resume(RUN)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    await engine.execute_run(RUN)
    assert len(prompts) == 2, store.run.data.get("error")
    assert "Support owns this product" in prompts[1]
    assert store.run.data["repair_budget"] == 0 and store.run.data["implementation_rounds"] == 1
    assert Journal.verify_chain([row.data for row in journal.records.rows])


async def answered_question(setup, attempt=1, *, role="implementer", stage="implement"):
    store, journal, actor = setup
    if stage == "review":
        store.run.data["assurance"] = {"review_agent": "bridge"}
    await journal.ensure_plan(RUN)
    await journal.intent(RUN, stage, attempt)
    await journal.worker_feedback(
        RUN,
        WorkerFeedback.model_validate(
            {
                "id": f"feedback-{attempt}",
                "role_id": role,
                "task_id": stage,
                "attempt": attempt,
                "disposition": "clarification",
                "summary": "Missing owner",
                "request": {
                    "id": f"question-{attempt}",
                    "kind": "product_definition",
                    "question": "Which owner?",
                    "context": "Human decision required",
                },
            }
        ),
    )
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        await journal.worker_response(
            RUN,
            f"question-{attempt}",
            WorkerResponse(id=f"answer-{attempt}", actor="Fictional human", answer="Support owns this product"),
        )
    finally:
        CURRENT_PRINCIPAL.reset(token)
    return store, journal, actor


@pytest.mark.parametrize("role,stage", [("architect", "prepare"), ("qa", "verify"), ("qa", "review")])
@pytest.mark.parametrize("owned", [False, True])
async def test_answered_other_stage_resumes_without_implementation_budget(clarification_setup, role, stage, owned):
    store, journal, actor = await answered_question(clarification_setup, role=role, stage=stage)
    token = CURRENT_PRINCIPAL.set(actor if owned else None)
    try:
        await journal.resume(RUN)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    task = await journal._task(RUN, stage)
    assert task.status == "queued" and task.attempt == 2
    assert store.run.status == "queued"
    assert await journal.human_clarification_continuation_allowance(RUN) == 0
    assert "human_clarification" not in next(r.data["payload"] for r in journal.records.rows if r.kind == "resume")


@pytest.mark.parametrize(
    "invalid",
    [
        "no-actor",
        "revoked",
        "missing-answer",
        "ambiguous-answer",
        "tampered-wal",
        "changed-request",
        "reserved-recovery",
        "consumed-recovery",
        "cancelled",
    ],
)
async def test_invalid_human_resume_does_not_queue_an_extra_attempt(clarification_setup, invalid):
    store, journal, actor = await answered_question(clarification_setup)
    if invalid == "revoked":
        store.identity_enabled = False
    elif invalid == "missing-answer":
        journal.records.rows[:] = [r for r in journal.records.rows if r.kind != "worker_response"]
    elif invalid == "ambiguous-answer":
        row = next(r for r in journal.records.rows if r.kind == "worker_response")
        await journal._append(
            RUN, "implement", "another-answer", "worker_response", {**row.data["payload"], "id": "second-answer"}
        )
    elif invalid == "tampered-wal":
        next(r for r in journal.records.rows if r.kind == "worker_response").data["payload"]["answer"] = "Changed"
    elif invalid == "changed-request":
        store.run.data["worker_request"]["context"] = "Changed question"
    elif invalid.endswith("-recovery"):
        store.run.data["provider_recovery"] = {"status": invalid.split("-", 1)[0]}
    elif invalid == "cancelled":
        store.run.data["cancel_request"] = {"status": "requested"}
    task = await journal._task(RUN, "implement")
    before = task.attempt
    token = CURRENT_PRINCIPAL.set(None if invalid == "no-actor" else actor)
    try:
        with pytest.raises((ValueError, AccessDenied)):
            await journal.resume(RUN)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert task.attempt == before and task.status == "paused" and store.run.status == "paused"
    assert not any(r.kind in {"resume", "handoff"} for r in journal.records.rows)


async def test_human_resume_is_single_use_and_survives_journal_restart(clarification_setup):
    store, journal, actor = await answered_question(clarification_setup)
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        await journal.resume(RUN)
        with pytest.raises(ValueError, match="Only a paused"):
            await journal.resume(RUN)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    reopened = Journal(store, plans=journal.plans, tasks=journal.tasks, records=journal.records)
    assert await reopened.human_clarification_continuation_allowance(RUN) == 1
    resume = next(r.data for r in journal.records.rows if r.kind == "resume")
    link = resume["payload"]["human_clarification"]
    assert link["from_attempt"] == 1 and link["to_attempt"] == 2
    assert link["resuming_actor"]["user_id"] == actor.id.hex
    await journal._append(RUN, "resume", "duplicated-resume", "resume", resume["payload"])
    with pytest.raises(ValueError, match="duplicated"):
        await reopened.human_clarification_continuation_allowance(RUN)


@pytest.mark.parametrize("drift", ["actor-context", "repository-scope"])
async def test_resume_rechecks_context_and_scope_after_admission_waits(clarification_setup, monkeypatch, drift):
    store, journal, actor = await answered_question(clarification_setup)
    if drift == "actor-context":
        original = ProjectAccess.authorize_actor

        async def authority(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            CURRENT_PRINCIPAL.set(None)
            return result

        monkeypatch.setattr(ProjectAccess, "authorize_actor", authority)
    else:

        async def campaign(*args, **kwargs):
            store.run.data["repository_id"] = uuid4().hex

        store.assert_campaign_current = campaign
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        with pytest.raises(AccessDenied, match="changed"):
            await journal.resume(RUN)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert store.run.status == "paused" and (await journal._task(RUN, "implement")).attempt == 1
    assert not any(r.kind in {"resume", "handoff"} for r in journal.records.rows)


async def test_fourth_attempt_human_question_cannot_create_an_unsealed_fifth(clarification_setup):
    store, journal, actor = await answered_question(clarification_setup, attempt=4)
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        with pytest.raises(ValueError, match="four ordinary"):
            await journal.resume(RUN)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    task = await journal._task(RUN, "implement")
    assert task.attempt == 4 and task.status == "paused"
    assert await journal.human_clarification_continuation_allowance(RUN) == 0


async def test_three_distinct_human_answers_remain_within_the_frozen_request_bound(clarification_setup):
    store, journal, actor = clarification_setup
    for attempt in range(1, 4):
        await answered_question(clarification_setup, attempt)
        token = CURRENT_PRINCIPAL.set(actor)
        try:
            await journal.resume(RUN)
        finally:
            CURRENT_PRINCIPAL.reset(token)
        assert await journal.human_clarification_continuation_allowance(RUN) == attempt
    assert (await journal._task(RUN, "implement")).attempt == 4
    with pytest.raises(ValueError, match="request budget"):
        await answered_question(clarification_setup, 4)
    assert await journal.human_clarification_continuation_allowance(RUN) == 3
    assert (await journal._task(RUN, "implement")).attempt == 4
    assert store.run.data["repair_budget"] == 0
