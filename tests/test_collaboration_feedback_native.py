# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native peer scoping and late-publication selection; requires isolated PostgreSQL."""

import uuid

from test_collaboration import configured

from firefly_forge.collaboration_persistence import ForgeWebhookInboxEntity
from firefly_forge.persistence import ForgeRepositoryEntity, ForgeRunEntity
from firefly_forge.store import identifier


async def test_native_feedback_peer_scope_and_post_success_publication_watch(store):
    service, repo = await configured(store)
    run_id = uuid.uuid4().hex
    selected = []
    async with store._unit():
        for number, kind, run, pending in [
            (12, "issue", run_id, False),
            (12, "issue", run_id, True),
            (13, "issue", run_id, True),
            (12, "pull_request", run_id, True),
            (12, "issue", uuid.uuid4().hex, True),
        ]:
            value = uuid.uuid4()
            data = dict(
                id=value.hex,
                status="processed",
                repository_id=repo["id"],
                provider="github",
                run_id=run,
                issue=dict(kind=kind, number=number),
                feedback_status="sent",
                feedback_stage="succeeded",
                feedback_publication_pending=pending,
            )
            await service.inbox.save(
                ForgeWebhookInboxEntity(
                    id=value,
                    repository_id=identifier(repo["id"]),
                    provider="github",
                    delivery_id=value.hex,
                    source_key=value.hex,
                    status="processed",
                    data=data,
                )
            )
            selected.append(data)
    async with store._unit():
        peers = await service.inbox.feedback_context(selected[0])
        pending = await service.inbox.find_pending()
    assert [row.id.hex for row in peers] == [selected[1]["id"], selected[0]["id"]]
    assert selected[1]["id"] in {row.id.hex for row in pending}
    assert selected[0]["id"] not in {row.id.hex for row in pending}


async def test_native_terminal_recovery_selection_is_scoped_typed_and_quiet_after_observation(store):
    service, repo = await configured(store)
    recovery = dict(request_id="a" * 32, task_key="b" * 32, attempt=4, status="consumed")
    observed = {"request_id": "a" * 32, "attempt": 4}
    previous = {"request_id": "c" * 32, "attempt": 3}
    cases = [
        ("new_recovery", recovery, previous),
        ("observed", recovery, observed),
        ("missing", None, previous),
        ("scalar", "invalid", previous),
        ("empty", {}, previous),
        ("bad_request", {**recovery, "request_id": "@everyone"}, previous),
        ("numeric_request", {**recovery, "request_id": 12345678901234567890123456789012}, previous),
        ("bool_attempt", {**recovery, "attempt": True}, previous),
        ("float_attempt", {**recovery, "attempt": 4.0}, previous),
        ("string_attempt", {**recovery, "attempt": "4"}, previous),
        ("zero_attempt", {**recovery, "attempt": 0}, previous),
        ("large_attempt", {**recovery, "attempt": 101}, previous),
        ("invalid_status", {**recovery, "status": "unknown"}, previous),
        ("scalar_status", {**recovery, "status": []}, previous),
        ("other_repository", recovery, previous),
        ("other_run", recovery, previous),
        ("unknown_write", recovery, previous),
        ("unknown_writing", recovery, previous),
    ]
    rows = {}
    async with store._unit():
        other_repository = uuid.uuid4()
        await store.repository_records.save(
            ForgeRepositoryEntity(id=other_repository, data={"id": other_repository.hex})
        )
        for name, value, retained in cases:
            run, receipt = uuid.uuid4(), uuid.uuid4()
            await store.run_records.save(
                ForgeRunEntity(
                    id=run,
                    repository_id=other_repository if name == "other_repository" else identifier(repo["id"]),
                    status="failed",
                    data={"id": run.hex, "status": "failed", "provider_recovery": value},
                )
            )
            status = "needs_reconciliation" if name == "unknown_writing" else "processed"
            data = dict(
                id=receipt.hex,
                status=status,
                repository_id=repo["id"],
                provider="github",
                run_id=uuid.uuid4().hex if name == "other_run" else run.hex,
                issue=dict(kind="issue", number=12),
                feedback_status=(
                    "needs_reconciliation"
                    if name == "unknown_write"
                    else "writing"
                    if name == "unknown_writing"
                    else "sent"
                ),
                feedback_stage="failed",
                feedback_intent={"provider_recovery": retained},
            )
            row = ForgeWebhookInboxEntity(
                id=receipt,
                repository_id=identifier(repo["id"]),
                provider="github",
                delivery_id=receipt.hex,
                source_key=receipt.hex,
                status=status,
                data=data,
            )
            await service.inbox.save(row)
            rows[name] = row
    async with store._unit():
        pending = await service.inbox.find_pending()
        assert {row.id for row in pending} == {rows["new_recovery"].id, rows["unknown_writing"].id}
        current = await service.inbox.find_by_id(rows["new_recovery"].id)
        current.data = {**current.data, "feedback_intent": {"provider_recovery": observed}}
        await service.inbox.save(current)
    async with store._unit():
        pending = await service.inbox.find_pending()
        assert [row.id for row in pending] == [rows["unknown_writing"].id]
    # This branch reconciles an interrupted intent without making a provider request.
    await service.sync_feedback()
    async with store._unit():
        assert await service.inbox.find_pending() == []
        unknown = await service.inbox.find_by_id(rows["unknown_writing"].id)
        assert unknown.data["feedback_status"] == "needs_reconciliation"
