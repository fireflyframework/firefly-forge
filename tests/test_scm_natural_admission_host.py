# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Same-answer admission uses the real journal and polling permission pipeline."""

import copy

import pytest
from test_scm_natural_intake_host import prepared
from test_scm_polling import poll_case as poll_case

from firefly_forge.agent_roles import WorkerFeedback, WorkerRequest
from firefly_forge.project_access import AccessDenied
from firefly_forge.scm_natural_effects import NaturalEffects, NaturalReadmission


async def question(c):
    request = WorkerRequest(
        id="question1",
        kind="other",
        question="Keep existing contacts?",
        context="Owner decision",
        choices=["Yes", "No"],
    )
    feedback = WorkerFeedback(
        id="public-question",
        role_id="implementer",
        task_id="implement",
        attempt=1,
        disposition="clarification",
        summary="Owner decision required",
        request=request,
    )
    c.db["runs"][c.run_id].data["worker_request"] = {
        **request.model_dump(),
        "status": "pending",
        "role_id": "implementer",
        "task_id": "implement",
        "attempt": 1,
    }
    journal = c.collaboration.deployments.engine.journal
    async with c.store._unit():
        await journal._append(
            c.run_id, "implement", "public-question", "worker_feedback", feedback.model_dump(mode="json")
        )


async def test_answer_intake_applies_once_through_original_respond_pipeline_without_resuming(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        await question(c)
        result = await service.process(receipt)
        assert result["natural_turn"]["state"] == "answer_pending_permission"
        effects = NaturalEffects(service)
        result = await effects.apply(receipt)
        assert result["natural_turn"]["state"] == "answer_saved_pending_resume"
        responses = [r.data for r in c.db["journal"].values() if r.kind == "worker_response"]
        assert len(responses) == 1
        assert responses[0]["payload"]["answer"] == "sí, conserva los contactos existentes"
        assert responses[0]["payload"]["responding_actor"]["provenance"]["external_actor_id"] == "7"
        assert c.db["runs"][c.run_id].data["status"] == "paused"
        await type(effects)(type(service)(c.collaboration)).apply(receipt)
        assert len([r for r in c.db["journal"].values() if r.kind == "worker_response"]) == 1
        assert c.state["natural_calls"] == 1


async def test_same_retained_answer_can_be_readmitted_after_known_permission_refusal_without_repost(
    poll_case,  # noqa: F811
    monkeypatch,  # noqa: F811
):  # noqa: F811
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        await question(c)
        await service.process(receipt)
        c.db["settings"][c.repository].data["channel_commands"] = []
        effects = NaturalEffects(service)
        result = await effects.apply(receipt)
        assert result["natural_turn"]["state"] == "answer_pending_permission"
        assert not [r for r in c.db["journal"].values() if r.kind == "worker_response"]
        c.db["settings"][c.repository].data["channel_commands"] = ["respond"]
        cut = await effects.review(receipt)
        result = await effects.readmit(
            receipt,
            NaturalReadmission(
                request_id="8" * 32, expected_intake_digest=cut["intake_digest"], expected_cut_digest=cut["digest"]
            ),
        )
        assert result["natural_turn"]["state"] == "answer_saved_pending_resume"
        assert len([r for r in c.db["journal"].values() if r.kind == "worker_response"]) == 1
        assert c.state["natural_calls"] == 1 and len(c.state["comments"]) == 3


async def test_derived_command_change_is_refused_before_native_authority(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        await question(c)
        await service.process(receipt)
        effects = NaturalEffects(service)
        cut = await effects.review(receipt)
        from firefly_forge.scm_natural_effects import answer_command

        parent = await c.collaboration.receipt(receipt)
        child = await effects._child(parent, cut, answer_command(parent), "answer", "7" * 32)
        c.db["inbox"][child["id"]].data["command"]["answer"] = "Different answer"
        changed = copy.deepcopy(c.db["inbox"][child["id"]].data)
        with pytest.raises((AccessDenied, ValueError)):
            await c.service._observation(c.project, c.repository, changed["id"])


async def test_normal_addressed_comment_reaches_durable_intake_without_command_or_conversation_ids(
    poll_case,  # noqa: F811
    monkeypatch,  # noqa: F811
):  # noqa: F811
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        await question(c)
        result = await c.collaboration.process(receipt)
        assert result.get("natural_turn", {}).get("human_message") == "sí, conserva los contactos existentes"
        assert c.state["natural_calls"] == 1
        assert len([r for r in c.db["journal"].values() if r.kind == "worker_response"]) == 1
        assert result["feedback_status"] == "sent"
        assert "@forge" not in c.state["posts"][-1]["body"]
        assert "/forge" not in c.state["posts"][-1]["body"]


async def test_readmission_never_replaces_an_unknown_derived_answer_intent(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        await question(c)
        await service.process(receipt)
        effects = NaturalEffects(service)
        cut = await effects.review(receipt)
        from firefly_forge.scm_natural_effects import answer_command

        parent = await c.collaboration.receipt(receipt)
        child = await effects._child(parent, cut, answer_command(parent), "answer", "7" * 32)
        row = c.db["inbox"][child["id"]]
        row.status = "needs_reconciliation"
        row.data = {**row.data, "status": row.status, "action_status": "writing"}
        cut = await effects.review(receipt)
        with pytest.raises(AccessDenied, match="unknown|reconcil"):
            await effects.readmit(
                receipt,
                NaturalReadmission(
                    request_id="8" * 32, expected_intake_digest=cut["intake_digest"], expected_cut_digest=cut["digest"]
                ),
            )
        assert len((await c.collaboration.receipt(receipt))["natural_effects"]) == 1
        assert not [r for r in c.db["journal"].values() if r.kind == "worker_response"]


async def test_readmitting_operator_is_rechecked_after_credential_wait(poll_case, monkeypatch):  # noqa: F811
    from types import SimpleNamespace
    from uuid import UUID, uuid4

    from firefly_forge.identity import Principal
    from firefly_forge.project_access import CURRENT_PRINCIPAL

    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        await question(c)
        await service.process(receipt)
        effects = NaturalEffects(service)
        cut = await effects.review(receipt)
        operator = Principal(
            uuid4(), "https://identity.invalid", "reviewing-operator", authentication_method="oidc_bearer"
        )
        c.db["users"][operator.id.hex] = SimpleNamespace(
            id=operator.id, issuer=operator.issuer, subject=operator.subject, enabled=True
        )
        c.db["memberships"]["reviewer"] = SimpleNamespace(
            user_id=operator.id, project_id=UUID(c.project), role="developer", active=True, revision=1
        )

        def revoke(value):
            if isinstance(value, dict) and value.get("request_id") == "8" * 32:
                c.db["users"][operator.id.hex].enabled = False

        c.state["admit_hook"] = revoke
        token = CURRENT_PRINCIPAL.set(operator)
        try:
            with pytest.raises(AccessDenied):
                await effects.readmit(
                    receipt,
                    NaturalReadmission(
                        request_id="8" * 32,
                        expected_intake_digest=cut["intake_digest"],
                        expected_cut_digest=cut["digest"],
                    ),
                )
        finally:
            CURRENT_PRINCIPAL.reset(token)
        assert not [r for r in c.db["journal"].values() if r.kind == "worker_response"]


async def unmentioned_worker(c, monkeypatch, *, ordering=None):
    service, addressed = await prepared(c, monkeypatch)
    await question(c)
    original = next(
        row for row in c.db["inbox"].values() if row.data["id"] != addressed and row.data.get("run_id") == c.run_id
    )
    await c.collaboration.feedback(original.data["id"])
    current = await c.collaboration.receipt(original.data["id"])
    assert current["feedback_status"] == "sent"
    assert current["feedback_intent"]["request_id"] == "question1"
    bot = c.comment(101, current["feedback_intent"]["body"])
    bot["user"]["type"] = "Bot"
    c.state["comments"].append(bot)
    c.state["clock"] += 3600
    human = c.comment(102, "Yes, keep the existing contacts.")
    if ordering is not None:
        from datetime import datetime, timedelta

        stamp = datetime.fromisoformat(bot["created_at"])
        delta = {"older": -1, "equal": 0, "newer": 1, "edited_older": -1}[ordering]
        human["created_at"] = (stamp + timedelta(seconds=delta)).isoformat()
        human["updated_at"] = (stamp + timedelta(seconds=1 if ordering == "edited_older" else delta)).isoformat()
    c.state["comments"].append(human)
    for _ in range(3):
        await c.service.step(c.repository)
    reply = next(row for row in c.db["inbox"].values() if row.source_key == "comment:102")
    assert reply.status == "received", reply.data.get("reason")
    return service, reply.data["id"]


async def test_unmentioned_worker_answer_uses_confirmed_question_and_native_respond_once(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        service, receipt = await unmentioned_worker(c, monkeypatch)
        result = await c.collaboration.process(receipt)
        assert result["natural_turn"]["state"] == "answer_saved_pending_resume"
        assert result["feedback_status"] == "sent", result.get("reason")
        assert len([row for row in c.db["journal"].values() if row.kind == "worker_response"]) == 1
        assert await type(service)(c.collaboration).process(receipt) == result
        assert c.state["natural_calls"] == 1


@pytest.mark.parametrize("boundary", ["before_effect", "after_effect"])
async def test_operator_reconciliation_is_not_overwritten_or_dispatched_after_wait(poll_case, monkeypatch, boundary):  # noqa: F811
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        await question(c)
        await service.process(receipt)
        effects = NaturalEffects(service)

        def reconcile():
            row = c.db["inbox"][receipt]
            row.data = {
                **row.data,
                "natural_turn": {**row.data["natural_turn"], "state": "needs_reconciliation"},
                "reason": "operator_review",
            }

        if boundary == "before_effect":
            old = effects._child

            async def child(*args, **kwargs):
                result = await old(*args, **kwargs)
                reconcile()
                return result

            monkeypatch.setattr(effects, "_child", child)
        else:
            old = c.collaboration._process_delegated

            async def delegated(*args, **kwargs):
                result = await old(*args, **kwargs)
                reconcile()
                return result

            monkeypatch.setattr(c.collaboration, "_process_delegated", delegated)
        result = await effects.apply(receipt)
        assert result["natural_turn"]["state"] == "needs_reconciliation"
        assert result["reason"] == "operator_review"
        assert len([row for row in c.db["journal"].values() if row.kind == "worker_response"]) == (
            0 if boundary == "before_effect" else 1
        )


async def test_original_retained_answer_cannot_claim_an_answer_saved_by_another_channel(poll_case, monkeypatch):  # noqa: F811
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        await question(c)
        await service.process(receipt)
        await c.collaboration.deployments.engine.journal.worker_response(
            c.run_id,
            "question1",
            {"id": "other-answer", "actor": "Current operator", "answer": "A different answer from another channel."},
        )
        with pytest.raises(AccessDenied, match="answer|receipt|custody"):
            await NaturalEffects(service).review(receipt)


@pytest.mark.parametrize("remapped", [False, True])
async def test_same_unmentioned_answer_readmits_under_a_renewed_current_binding_without_repost(
    poll_case, monkeypatch, remapped
):  # noqa: F811
    from uuid import uuid4

    async with poll_case() as c:
        service, receipt = await unmentioned_worker(c, monkeypatch)
        c.db["settings"][c.repository].data["channel_commands"] = []
        pending = await c.collaboration.process(receipt)
        assert pending["natural_turn"]["state"] == "answer_pending_permission"
        old_digest = pending["natural_turn"]["context"]["scope"]["binding_digest"]
        request = c.state["consent_request"].model_copy(
            update={"request_id": uuid4().hex, "expected_previous_digest": old_digest}
        )
        if remapped:
            member = copy.deepcopy(c.db["memberships"]["member"])
            member.user_id = c.owner.id
            c.db["memberships"]["replacement"] = member
            request = request.model_copy(update={"native_user_id": c.owner.id.hex})
        renewed = await c.service.configure_binding(c.project, c.repository, request)
        assert renewed["digest"] != old_digest
        c.db["settings"][c.repository].data["channel_commands"] = ["respond"]
        effects = NaturalEffects(service)
        if remapped:
            with pytest.raises(AccessDenied, match="owner mapping"):
                await effects.review(receipt)
            assert not [row for row in c.db["journal"].values() if row.kind == "worker_response"]
            assert c.state["natural_calls"] == 1
            return
        cut = await effects.review(receipt)
        assert cut["scopes"][0]["binding_digest"] == renewed["digest"]
        result = await effects.readmit(
            receipt,
            NaturalReadmission(
                request_id="8" * 32, expected_intake_digest=cut["intake_digest"], expected_cut_digest=cut["digest"]
            ),
        )
        assert result["natural_turn"]["state"] == "answer_saved_pending_resume"
        assert len([row for row in c.db["journal"].values() if row.kind == "worker_response"]) == 1
        assert c.state["natural_calls"] == 1


@pytest.mark.parametrize("ordering", ["older", "equal", "newer", "edited_older"])
async def test_a_question_must_precede_an_unmentioned_answer_before_any_model_or_effect(
    poll_case, monkeypatch, ordering
):  # noqa: F811
    async with poll_case() as c:
        service, receipt = await unmentioned_worker(c, monkeypatch, ordering=ordering)
        result = await c.collaboration.process(receipt)
        responses = [entry for entry in c.db["journal"].values() if entry.kind == "worker_response"]
        if ordering == "newer":
            assert result["natural_turn"]["state"] == "answer_saved_pending_resume"
            assert c.state["natural_calls"] == 1 and len(responses) == 1
        else:
            assert result["status"] == "denied" and result["action_status"] == "not_dispatched"
            assert c.state["natural_calls"] == 0 and not responses
