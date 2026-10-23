# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real PostgreSQL message projection/current ACL; no provider, commands or source edits."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_worker_reply_authority_native import answer, pending

from firefly_forge.agent_roles import WorkerFeedback, WorkerResponse
from firefly_forge.persistence import ForgeJournalEntity
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess


@pytest.mark.parametrize("revoked", [False, True])
async def test_native_retained_answers_ignore_large_unrelated_wal_and_recheck_reader(
    store, tmp_path, monkeypatch, revoked
):
    run, journal, actor, identity, _ = await pending(store, tmp_path)
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        first = await journal.worker_response(
            run["id"],
            "question-1",
            answer().model_copy(update={"answer": "UndefinedColumnError: normalized_name is absent"}),
        )
        second = WorkerFeedback(
            id="feedback-2",
            role_id="implementer",
            task_id="implement",
            attempt=2,
            disposition="clarification",
            summary="A separate source-receipt decision",
            request={
                "id": "question-2",
                "kind": "other",
                "question": "May work use the reviewed source scope?",
                "context": "The earlier diagnostic remains relevant",
            },
        )
        await journal.worker_feedback(run["id"], second)
        latest = await journal.worker_response(
            run["id"],
            "question-2",
            WorkerResponse(id="answer-2", actor="Operator", answer="Keep the original command and read limits"),
        )
        async with store._unit():
            for _ in range(8):
                await journal._append(run["id"], "implement", uuid4().hex, "tool_receipt", {"output": "x" * 600000})
        async with store._unit() as session:
            before = await session.scalar(select(func.count()).where(ForgeJournalEntity.run_id == UUID(run["id"])))
        original = store.admit_credentials

        async def admit(value):
            await original(value)
            if revoked:
                await ProjectAccess(store).set_membership(run["project_id"], actor, None)

        monkeypatch.setattr(store, "admit_credentials", admit)
        if revoked:
            with pytest.raises(AccessDenied):
                await journal.implementation_context(run["id"])
        else:
            context = await journal.implementation_context(run["id"])
            assert len(context["pairs"]) == 2
            assert context["pairs"][0]["response"]["digest"] == first["digest"]
            assert "UndefinedColumnError" in context["pairs"][0]["answer"]
            assert context["current_response_digest"] == latest["digest"]
            assert context["pairs"][1]["answer"] == latest["payload"]["answer"]
            assert context["historical_only"] is True and context["grants_authority"] is False
        CURRENT_PRINCIPAL.reset(token)
        token = CURRENT_PRINCIPAL.set(None)
        async with store._unit() as session:
            after = await session.scalar(select(func.count()).where(ForgeJournalEntity.run_id == UUID(run["id"])))
        assert after == before
    finally:
        CURRENT_PRINCIPAL.reset(token)
        await identity.client.aclose()
