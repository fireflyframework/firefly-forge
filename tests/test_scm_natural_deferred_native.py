# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native PostgreSQL selection and scheduler custody; the effect callback is controlled."""

import copy
from uuid import uuid4

import pytest
from test_collaboration import configured

from firefly_forge.collaboration import Collaboration
from firefly_forge.collaboration_persistence import ForgeWebhookInboxEntity
from firefly_forge.native_question_publications import NativeQuestionPublications
from firefly_forge.scm_natural_effects import NaturalEffects, answer_command, projected_delivery
from firefly_forge.scm_natural_tasks import natural_policy_digest
from firefly_forge.store import identifier


@pytest.mark.parametrize("child_state", ["completed", "denied", "unknown", "missing"])
async def test_native_sent_parent_is_durably_selected_and_only_exact_completed_child_catches_up(
    store, monkeypatch, child_state
):
    original, repo = await configured(store)
    parent_id, run_id = uuid4().hex, uuid4().hex
    parent = dict(
        id=parent_id,
        repository_id=repo["id"],
        provider="github",
        remote_id="42",
        actor="7",
        digest="a" * 64,
        issue=dict(kind="issue", number=3),
        verification=dict(transport="controlled_scheduler_fixture"),
        source_snapshot=dict(body="Keep existing contacts."),
        status="processed",
        feedback_status="sent",
        feedback_stage="action_completed",
        natural_turn=dict(
            version=1,
            state="answer_pending_permission",
            human_message="Keep existing contacts.",
            policy_digest=natural_policy_digest(),
            resolution=dict(state="answer", run_id=run_id, question_id="contacts", answer="Keep existing contacts."),
        ),
    )
    child = projected_delivery(parent, answer_command(parent), stage="answer", admission="b" * 32)
    parent["natural_effects"] = {child["id"]: child["natural_parent"]}
    child.update(
        status="processed"
        if child_state == "completed"
        else "denied"
        if child_state == "denied"
        else "needs_reconciliation",
        action_status="completed"
        if child_state == "completed"
        else "not_dispatched"
        if child_state == "denied"
        else "needs_reconciliation",
    )
    async with store._unit():
        for data in [parent, *([child] if child_state != "missing" else [])]:
            await original.inbox.save(
                ForgeWebhookInboxEntity(
                    id=identifier(data["id"]),
                    repository_id=identifier(repo["id"]),
                    provider="github",
                    delivery_id=data["id"],
                    source_key=data["id"],
                    status=data["status"],
                    data=copy.deepcopy(data),
                )
            )
    restarted = Collaboration(store, original.scm, inbox=type(original.inbox)())
    async with store._unit():
        selected = await restarted.inbox.find_pending()
    assert parent_id in {row.id.hex for row in selected}, "A sent action_completed parent must survive selection"
    calls = []

    async def apply(self, receipt_id, *, admission=None, explicit=False):
        # Native authority/effect admission is covered separately; this callback witnesses scheduler identity only.
        calls.append((receipt_id, admission, explicit))
        current = await self.collaboration.receipt(receipt_id)
        return await self._retain_state(receipt_id, "answer_saved_pending_resume", expected=current)

    async def no_effect(*args, **kwargs):
        return None

    monkeypatch.setattr(NaturalEffects, "apply", apply)
    monkeypatch.setattr(NativeQuestionPublications, "sync", no_effect)
    monkeypatch.setattr(restarted, "feedback", no_effect)
    await restarted.sync_feedback()
    current = await restarted.receipt(parent_id)
    if child_state == "completed":
        assert calls == [(parent_id, "b" * 32, False)]
        assert current["natural_turn"]["state"] == "answer_saved_pending_resume"
    elif child_state == "unknown":
        assert calls == [] and current["natural_turn"]["state"] == "needs_reconciliation"
    else:
        assert calls == [] and current == parent
    await restarted.sync_feedback()
    assert len(calls) == (1 if child_state == "completed" else 0)
