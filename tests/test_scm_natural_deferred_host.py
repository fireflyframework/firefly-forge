# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The retained natural parent remains usable while its exact native child waits."""

import time
from types import SimpleNamespace

import pytest
from test_scm_natural_admission_host import question
from test_scm_natural_intake_host import prepared

from firefly_forge.scm_actor_authority import SCMActorAuthority
from firefly_forge.scm_natural_effects import NaturalEffects
from firefly_forge.scm_provider_backoff import ProviderRequestDeferred

pytest_plugins = ["test_scm_polling"]


async def test_natural_parent_keeps_exact_deferred_child_then_saves_answer_once(poll_case, monkeypatch):
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        await question(c)
        await service.process(receipt)
        effects = NaturalEffects(service)
        original = SCMActorAuthority.resolve
        deadline = time.time() + 60
        calls = []

        async def defer_once(self, delivery):
            if delivery.get("natural_parent"):
                calls.append(delivery["id"])
                if len(calls) == 1:
                    raise ProviderRequestDeferred("Local provider request is occupied", retry_at=deadline)
            return await original(self, delivery)

        monkeypatch.setattr(SCMActorAuthority, "resolve", defer_once)
        pending = await effects.apply(receipt)
        assert pending["natural_turn"]["state"] == "answer_pending_permission"
        assert len(pending["natural_effects"]) == 1
        child_id = next(iter(pending["natural_effects"]))
        child = await c.collaboration.receipt(child_id)
        assert child["status"] == "received" and child["action_status"] == "not_dispatched"
        assert not [r for r in c.db["journal"].values() if r.kind == "worker_response"]
        await type(effects)(type(service)(c.collaboration)).apply(receipt)
        assert calls == [child_id]
        monkeypatch.setattr("firefly_forge.collaboration.time", SimpleNamespace(time=lambda: deadline + 1))
        saved = await type(effects)(type(service)(c.collaboration)).apply(receipt)
        assert saved["natural_turn"]["state"] == "answer_saved_pending_resume"
        assert list(saved["natural_effects"]) == [child_id]
        responses = [r for r in c.db["journal"].values() if r.kind == "worker_response"]
        assert len(responses) == 1 and calls == [child_id, child_id]
        assert c.state["natural_calls"] == 1 and c.db["runs"][c.run_id].data["status"] == "paused"
        await effects.apply(receipt)
        assert len([r for r in c.db["journal"].values() if r.kind == "worker_response"]) == 1
        assert c.state["natural_calls"] == 1


@pytest.mark.parametrize("outcome", ["plain_received", "forged_wait", "unknown"])
async def test_natural_parent_does_not_treat_unproven_or_unknown_child_as_wait(poll_case, monkeypatch, outcome):
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        await question(c)
        await service.process(receipt)

        async def held(self, delivery):
            raise ProviderRequestDeferred("Local wait", retry_at=time.time() + 60)

        monkeypatch.setattr(SCMActorAuthority, "resolve", held)
        original = c.collaboration._process_delegated

        async def corrupt(child_id):
            result = await original(child_id)
            row = c.db["inbox"][child_id]
            if outcome == "plain_received":
                row.data["action_deferred"] = None
            elif outcome == "forged_wait":
                row.data["action_deferred"]["outcome"] = "unknown"
            else:
                row.status = row.data["status"] = "needs_reconciliation"
                row.data["action_status"] = "needs_reconciliation"
            return result

        monkeypatch.setattr(c.collaboration, "_process_delegated", corrupt)
        result = await NaturalEffects(service).apply(receipt)
        assert result["natural_turn"]["state"] == "needs_reconciliation"
        assert not [r for r in c.db["journal"].values() if r.kind == "worker_response"]
        assert c.state["natural_calls"] == 1


async def test_sync_after_restart_finishes_exact_answer_parent_even_after_transient_review_wait(poll_case, monkeypatch):
    from firefly_forge.native_question_publications import NativeQuestionPublications

    async def no_new_questions(self):
        return None

    monkeypatch.setattr(NativeQuestionPublications, "sync", no_new_questions)
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        await question(c)
        await service.process(receipt)
        original = SCMActorAuthority.resolve
        deadline = time.time() + 60
        child_calls = []

        async def defer_once(self, delivery):
            if delivery.get("natural_parent"):
                child_calls.append(delivery["id"])
                if len(child_calls) == 1:
                    raise ProviderRequestDeferred("Local provider wait", retry_at=deadline)
            return await original(self, delivery)

        monkeypatch.setattr(SCMActorAuthority, "resolve", defer_once)
        pending = await NaturalEffects(service).apply(receipt)
        child_id = next(iter(pending["natural_effects"]))
        parent_row = c.db["inbox"][receipt]
        parent_row.data.update(feedback_status="sent", feedback_stage="action_completed")
        await c.collaboration.sync_feedback()
        assert child_calls == [child_id]
        assert (await c.collaboration.receipt(receipt))["natural_turn"]["state"] == "answer_pending_permission"
        monkeypatch.setattr("firefly_forge.collaboration.time", SimpleNamespace(time=lambda: deadline + 1))
        original_review = NaturalEffects.review
        reviews = []

        async def once_held(self, *args, **kwargs):
            reviews.append(True)
            if len(reviews) == 1:
                raise ProviderRequestDeferred("The next read is held locally")
            return await original_review(self, *args, **kwargs)

        monkeypatch.setattr(NaturalEffects, "review", once_held)
        await c.collaboration.sync_feedback()
        assert (await c.collaboration.receipt(child_id))["action_status"] == "completed"
        assert (await c.collaboration.receipt(receipt))["natural_turn"]["state"] == "answer_pending_permission"
        await c.collaboration.sync_feedback()
        saved = await c.collaboration.receipt(receipt)
        assert saved["natural_turn"]["state"] == "answer_saved_pending_resume"
        assert list(saved["natural_effects"]) == [child_id]
        assert c.state["natural_calls"] == 1 and child_calls == [child_id, child_id]
        assert len([r for r in c.db["journal"].values() if r.kind == "worker_response"]) == 1
        await c.collaboration.sync_feedback()
        assert len([r for r in c.db["journal"].values() if r.kind == "worker_response"]) == 1


async def test_native_pending_query_includes_sent_natural_parent_with_action_completed_stage():
    from sqlalchemy.dialects import postgresql

    from firefly_forge.collaboration_persistence import ForgeWebhookInboxRepository

    statements = []

    async def scalars(statement):
        statements.append(str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})))
        return []

    repository = ForgeWebhookInboxRepository(session=SimpleNamespace(scalars=scalars))
    assert await repository.find_pending() == []
    sql = statements[0]
    assert "'natural_turn'" in sql and "'answer_pending_permission'" in sql
    assert "'resolution'" in sql and "'answer'" in sql and "'natural_effects'" in sql
    assert "jsonb_typeof" in sql and "LIMIT 200" in sql


async def test_sync_unknown_child_only_projects_parent_hold_without_retry_or_apply(poll_case, monkeypatch):
    from firefly_forge.native_question_publications import NativeQuestionPublications

    async def no_new_questions(self):
        return None

    monkeypatch.setattr(NativeQuestionPublications, "sync", no_new_questions)
    async with poll_case() as c:
        service, receipt = await prepared(c, monkeypatch)
        await question(c)
        await service.process(receipt)

        async def held(self, delivery):
            raise ProviderRequestDeferred("Local provider wait", retry_at=time.time() + 60)

        monkeypatch.setattr(SCMActorAuthority, "resolve", held)
        pending = await NaturalEffects(service).apply(receipt)
        child_id = next(iter(pending["natural_effects"]))
        row = c.db["inbox"][child_id]
        row.status = row.data["status"] = "needs_reconciliation"
        row.data["action_status"] = "needs_reconciliation"
        row.data["feedback_status"] = "not_requested"

        async def forbidden(*args, **kwargs):
            raise AssertionError("An uncertain child may only project the parent's held state")

        monkeypatch.setattr(NaturalEffects, "apply", forbidden)
        monkeypatch.setattr(c.collaboration, "_process_delegated", forbidden)
        await c.collaboration.sync_feedback()
        assert (await c.collaboration.receipt(receipt))["natural_turn"]["state"] == "needs_reconciliation"
        assert not [r for r in c.db["journal"].values() if r.kind == "worker_response"]
        assert c.state["natural_calls"] == 1
