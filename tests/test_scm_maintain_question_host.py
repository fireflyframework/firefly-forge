# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Configured maintain questions use the existing durable native answer path."""

import copy
from datetime import UTC, datetime, timedelta

import pytest
from test_scm_maintain_ticket_host import poll_case as poll_case
from test_scm_maintain_ticket_host import prepared_binding

from firefly_forge.agent_roles import WorkerFeedback
from firefly_forge.scm_natural_models import NaturalDecision


async def question(case, run_id):
    feedback = WorkerFeedback(
        id="maintain-feedback",
        role_id="implementer",
        task_id="implement",
        attempt=1,
        disposition="clarification",
        summary="A decision is required for this maintenance unit",
        request={
            "id": "maintain-question",
            "kind": "other",
            "question": "Keep the existing contacts?",
            "context": "This fresh maintenance unit preserves the original records.",
            "choices": ["Keep", "Review"],
        },
    )
    journal = case.collaboration.deployments.engine.journal
    await journal._append(run_id, "implement", "maintain-feedback", "worker_feedback", feedback.model_dump(mode="json"))
    case.db["runs"][run_id].status = "paused"
    case.db["runs"][run_id].data.update(
        status="paused",
        active_task="implement",
        pause={"kind": "worker_request"},
        role_snapshot={"max_requests": 3},
        worker_request={
            **feedback.request.model_dump(),
            "role_id": "implementer",
            "task_id": "implement",
            "attempt": 1,
            "status": "pending",
        },
    )


@pytest.mark.parametrize("change", [None, "revoke", "expire"])
async def test_native_maintain_question_and_owner_reply_save_only_the_child_answer(
    poll_case, monkeypatch, tmp_path, change
):
    from test_communication_writer import configured_store

    async with poll_case() as c:
        service, request = await prepared_binding(c, monkeypatch)
        parent = copy.deepcopy(c.db["runs"][c.run_id].data)
        kept = await service.configure(c.project, c.repository, request)
        await question(c, request.run_id)
        await c.collaboration.feedback(kept["outbox_id"])
        sent = await c.collaboration.receipt(kept["outbox_id"])
        assert sent["feedback_status"] == "sent", sent
        assert sent["feedback_intent"]["request_id"] == "maintain-question"
        c.store.task_router = (await configured_store(tmp_path / "models")).task_router
        from types import SimpleNamespace

        async def old_resume_grant(*args, **kwargs):
            raise AssertionError("Answer-only maintenance must not consult an unrelated resume grant")

        c.collaboration.poll_resume_authority = SimpleNamespace(_grant=old_resume_grant)
        seen = []

        async def classify(message, context, settings):
            seen.append(context)
            if change == "revoke":
                c.db["users"][c.user_id].enabled = False
            elif change == "expire":
                import firefly_forge.scm_maintain_tickets as module

                monkeypatch.setattr(module, "clock_ms", lambda: kept["expires_at_ms"])
            return NaturalDecision(intent="answer", message="Your answer is retained.")

        monkeypatch.setattr("firefly_forge.scm_natural_tasks.classify", classify)
        stamp = datetime.now(UTC)
        bot = c.comment(101, sent["feedback_intent"]["body"])
        bot["user"]["type"] = "Bot"
        bot.update(created_at=stamp.isoformat(), updated_at=stamp.isoformat())
        owner = c.comment(502, "Keep the existing contacts.")
        owner.update(
            created_at=(stamp + timedelta(seconds=1)).isoformat(), updated_at=(stamp + timedelta(seconds=1)).isoformat()
        )
        c.state["comments"].extend([bot, owner])
        c.state["clock"] += 3600
        for _ in range(3):
            await c.service.step(c.repository)
        reply = next(r.data for r in c.db["inbox"].values() if r.source_key == "comment:502")
        result = await c.collaboration.process(reply["id"])
        child = next(row for row in seen[0]["candidates"] if row["run_id"] == request.run_id)
        assert child.get("origin") == "worker_journal" and child["question_id"] == "maintain-question", child
        assert child["publication"]["receipt_id"] == kept["outbox_id"]
        assert "read_only" not in child
        if change is not None:
            assert result["natural_turn"]["state"] == "needs_reconciliation"
            assert not [r for r in c.db["journal"].values() if r.kind == "worker_response"]
            assert c.db["runs"][request.run_id].data["worker_request"]["status"] == "pending"
            assert c.db["runs"][c.run_id].data == parent
            return
        assert result["natural_turn"]["state"] == "answer_saved_pending_resume", result.get("natural_turn")
        responses = [r.data for r in c.db["journal"].values() if r.kind == "worker_response"]
        assert len(responses) == 1 and responses[0]["run_id"] == request.run_id
        assert responses[0]["payload"]["answer"] == "Keep the existing contacts."
        assert c.db["runs"][request.run_id].data["status"] == "paused"
        assert c.db["runs"][c.run_id].data == parent
        assert len(seen) == 1
        assert not [r for r in c.db["journal"].values() if r.kind in {"resume", "provider_dispatch_intent"}]


async def test_current_native_anchor_remains_proactive_after_the_owner_answer_receipt(poll_case, monkeypatch):
    from types import SimpleNamespace
    from uuid import UUID, uuid4

    async with poll_case() as c:
        service, request = await prepared_binding(c, monkeypatch)
        kept = await service.configure(c.project, c.repository, request)
        await c.collaboration.feedback(kept["outbox_id"])
        anchor = await c.collaboration.receipt(kept["outbox_id"])
        reply_id = uuid4().hex
        later = {
            **copy.deepcopy(anchor),
            "id": reply_id,
            "maintain_ticket": None,
            "natural_turn": {"state": "answer_saved_pending_resume"},
        }
        c.db["inbox"][reply_id] = SimpleNamespace(
            id=UUID(reply_id),
            repository_id=UUID(c.repository),
            provider="github",
            status="processed",
            sequence=1000,
            data=later,
        )
        assert await c.collaboration._feedback_head(anchor, proactive=True)
        c.db["inbox"][reply_id].data["feedback_status"] = "needs_reconciliation"
        assert not await c.collaboration._feedback_head(anchor, proactive=True)


async def test_question_notifications_require_the_exact_immutable_native_feedback(poll_case, monkeypatch):
    from firefly_forge.project_access import AccessDenied

    async with poll_case() as c:
        service, request = await prepared_binding(c, monkeypatch)
        kept = await service.configure(c.project, c.repository, request)
        await question(c, request.run_id)
        anchor = await c.collaboration.receipt(kept["outbox_id"])
        assert (await service.current(anchor))["run"]["worker_request"]["id"] == "maintain-question"
        c.db["journal"].clear()
        with pytest.raises(AccessDenied):
            await service.current(anchor)


def test_answer_only_maintain_reference_cannot_resolve_a_resume():
    from firefly_forge.scm_natural_models import resolve_task

    scope = dict(
        repository_id="a" * 32,
        project_id="b" * 32,
        issue={"kind": "issue", "number": 3},
        actor="7",
        binding_digest="c" * 64,
        native_user_id="d" * 32,
    )
    row = {
        **scope,
        "run_id": "e" * 32,
        "question_id": "exact-question",
        "question_digest": "f" * 64,
        "run_status": "paused",
        "question_status": "answered",
        "response_digest": "1" * 64,
        "consumed": False,
        "origin": "worker_journal",
        "answer_only": True,
    }
    decision = NaturalDecision(intent="resume", message="The owner asks for continuation.")
    held = resolve_task(scope, [row], decision, answer="Resume")
    assert held.state == "waiting" and held.reason == "answer_only_reference"
    ordinary = {k: v for k, v in row.items() if k != "answer_only"}
    assert resolve_task(scope, [ordinary], decision, answer="Resume").state == "resume"


async def test_known_unclassified_setup_reply_does_not_hide_a_real_worker_question(poll_case, monkeypatch):
    from firefly_forge.scm_natural_tasks import SCMNaturalTasks

    async with poll_case() as c:
        service, request = await prepared_binding(c, monkeypatch)
        kept = await service.configure(c.project, c.repository, request)
        await question(c, request.run_id)
        await c.collaboration.feedback(kept["outbox_id"])
        unclassified = await c.collaboration.process(request.observed_receipt_id)
        assert unclassified["natural_turn"]["state"] == "waiting_configuration"
        assert unclassified["feedback_status"] == "sent" and "decision" not in unclassified["natural_turn"]
        delivery = {
            "id": "0" * 32,
            "repository_id": c.repository,
            "actor": "7",
            "provider": "github",
            "issue": {"kind": "issue", "number": 3},
        }
        context_scope = kept["polling_scope"]
        projected = await SCMNaturalTasks(c.collaboration).reply_source(
            delivery, [context_scope], "Keep the existing contacts."
        )
        assert projected is not None
        assert [ref.run_id for ref in projected.question_refs] == [request.run_id]
        assert projected.question_refs[0].source_receipt_id == kept["outbox_id"]
        c.db["inbox"][unclassified["id"]].data["natural_turn"]["classifier_outcome"] = "confirmed_valid"
        with pytest.raises(KeyError, match="decision"):
            await SCMNaturalTasks(c.collaboration).reply_source(
                delivery, [context_scope], "Keep the existing contacts."
            )


def test_two_pending_native_questions_require_grounding_in_the_human_reply():
    from firefly_forge.scm_natural_models import resolve_task

    scope = dict(
        repository_id="a" * 32,
        project_id="b" * 32,
        issue={"kind": "issue", "number": 3},
        actor="7",
        binding_digest="c" * 64,
        native_user_id="d" * 32,
    )
    parent = {
        **scope,
        "run_id": "1" * 32,
        "question_id": "parent-question",
        "question_digest": "f" * 64,
        "question": "Keep this source?",
        "run_status": "paused",
        "question_status": "pending",
        "consumed": False,
    }
    child = {
        **scope,
        "run_id": "2" * 32,
        "question_id": "child-question",
        "question_digest": "e" * 64,
        "question": "Keep the existing contacts?",
        "run_status": "paused",
        "question_status": "pending",
        "consumed": False,
        "answer_only": True,
    }
    bare = NaturalDecision(intent="answer", message="The answer is retained.")
    assert resolve_task(scope, [parent, child], bare, answer="Keep the existing contacts.").state == "waiting"
    grounded = NaturalDecision(
        intent="answer",
        message="The answer is retained.",
        choice=2,
        selection_evidence={"human_span": "existing contacts", "question_span": "existing contacts"},
    )
    result = resolve_task(scope, [parent, child], grounded, answer="Keep the existing contacts.")
    assert result.state == "answer" and result.run_id == child["run_id"]
