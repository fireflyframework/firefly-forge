# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exact committed decisions can be observed without replaying learning writes."""

import copy
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from test_learning_ledger import completed

from firefly_forge.identity import Principal
from firefly_forge.journal import digest
from firefly_forge.learning import LearningLedger
from firefly_forge.learning_persistence import ForgeLearningEventEntity
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess


@contextmanager
def acting(actor):
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        yield
    finally:
        CURRENT_PRINCIPAL.reset(token)


async def case(store, tmp_path):
    engine, run = await completed(store, tmp_path, scoped=True)
    ledger = LearningLedger(store, engine.journal)
    ledger.engine = engine
    record = await ledger.capture_outcome(run["id"])
    reviewer = Principal(uuid4(), "https://issuer.invalid", "owned-reviewer")
    await ProjectAccess(store).set_membership(run["project_id"], reviewer, "reviewer")
    return ledger, record, run, reviewer


async def retained(ledger, record):
    async with ledger.store._unit():
        return [
            (row.id.hex, copy.deepcopy(row.data), row.digest)
            for row in await ledger.events.for_record(UUID(record["id"]))
        ]


async def test_new_invalidation_retains_its_actual_actor_and_original_digest(store, tmp_path):
    ledger, record, _, reviewer = await case(store, tmp_path)
    request = uuid4().hex
    with acting(reviewer):
        await ledger.invalidate(record["id"], reason="Declared dependency changed", request_id=request)
    event = (await retained(ledger, record))[-1][1]
    assert event["actor_id"] == reviewer.id.hex
    assert event["digest"] == digest({key: value for key, value in event.items() if key != "digest"})


async def test_fresh_ledger_recovers_exact_committed_request_without_repeating_write(store, tmp_path):
    ledger, record, run, reviewer = await case(store, tmp_path)
    request = uuid4().hex
    with acting(reviewer):
        await ledger.invalidate(record["id"], reason="Retained source changed", request_id=request)
    before = await retained(ledger, record)
    fresh = LearningLedger(store, ledger.journal)
    with acting(reviewer):
        receipt = await fresh.invalidation_receipt(record["id"], request)
    event_id, event, original_digest = before[-1]
    assert receipt == {
        "event_id": event_id,
        "record_id": record["id"],
        "repository_id": run["repository_id"],
        "run_id": run["id"],
        "request_id": request,
        "kind": "invalidation",
        "event_state": "invalidated",
        "current_record_state": "invalidated",
        "reason": "Retained source changed",
        "actor_id": reviewer.id.hex,
        "created_at": event["created_at"],
        "sequence": event["sequence"],
        "digest": original_digest,
    }
    assert await retained(ledger, record) == before


async def test_other_request_state_and_unobserved_request_do_not_confirm_pending_identity(store, tmp_path):
    ledger, record, _, reviewer = await case(store, tmp_path)
    pending, competing = uuid4().hex, uuid4().hex
    with acting(reviewer):
        await ledger.invalidate(record["id"], reason="Another explicit decision", request_id=competing)
        before = await retained(ledger, record)
        with pytest.raises(KeyError, match="not observed"):
            await ledger.invalidation_receipt(record["id"], pending)
    assert (await ledger.records(record["envelope"]["context"]["repository_id"]))[0]["state"] == "invalidated"
    assert await retained(ledger, record) == before


async def test_duplicate_and_reason_collision_preserve_the_original_actor_event(store, tmp_path):
    ledger, record, run, reviewer = await case(store, tmp_path)
    request = uuid4().hex
    with acting(reviewer):
        await ledger.invalidate(record["id"], reason="Reviewed dependency drift", request_id=request)
    before = await retained(ledger, record)
    second = Principal(uuid4(), "https://issuer.invalid", "another-reviewer")
    await ProjectAccess(store).set_membership(run["project_id"], second, "reviewer")
    with acting(second):
        await ledger.invalidate(record["id"], reason="Reviewed dependency drift", request_id=request)
        with pytest.raises(ValueError, match="another decision"):
            await ledger.invalidate(record["id"], reason="Different decision", request_id=request)
        receipt = await ledger.invalidation_receipt(record["id"], request)
    assert receipt["actor_id"] == reviewer.id.hex
    assert await retained(ledger, record) == before


async def test_legacy_event_attribution_is_unknown_without_rewriting_bytes_on_read_or_duplicate(store, tmp_path):
    ledger, record, _, reviewer = await case(store, tmp_path)
    request = uuid4().hex
    async with store._unit():
        row = await ledger.records_repository.find_by_id(UUID(record["id"]))
        history = await ledger.events.for_record(row.id)
        data = {
            "record_id": row.id.hex,
            "run_id": row.run_id.hex,
            "sequence": len(history) + 1,
            "request_id": request,
            "kind": "invalidation",
            "payload": {"reason": "Legacy reviewed decision", "state": "invalidated"},
            "created_at": (await ledger.records_repository.database_time()).isoformat(),
            "previous_hash": history[-1].digest,
        }
        data["digest"] = digest(data)
        await ledger.events.save(
            ForgeLearningEventEntity(
                id=uuid4(),
                record_id=row.id,
                sequence=data["sequence"],
                request_id=request,
                digest=data["digest"],
                data=data,
            )
        )
        row.state = "invalidated"
        await ledger.records_repository.save(row)
    before = await retained(ledger, record)
    with acting(reviewer):
        receipt = await ledger.invalidation_receipt(record["id"], request)
        await ledger.invalidate(record["id"], reason="Legacy reviewed decision", request_id=request)
    assert receipt["actor_id"] is None and receipt["digest"] == before[-1][2]
    assert await retained(ledger, record) == before


@pytest.mark.parametrize("authority", ["viewer", "revoked", "suspended", "foreign"])
async def test_receipt_requires_current_review_authority(store, tmp_path, authority):
    from firefly_forge.identity_persistence import ForgeIdentityUserRepository

    ledger, record, run, reviewer = await case(store, tmp_path)
    request = uuid4().hex
    with acting(reviewer):
        await ledger.invalidate(record["id"], reason="Review scope", request_id=request)
    access = ProjectAccess(store)
    if authority in {"viewer", "revoked"}:
        await access.set_membership(run["project_id"], reviewer, "viewer" if authority == "viewer" else None)
    elif authority == "suspended":
        async with store._unit():
            user = await ForgeIdentityUserRepository().find_by_id(reviewer.id)
            user.enabled = False
            await ForgeIdentityUserRepository().save(user)
    else:
        reviewer = Principal(uuid4(), "https://issuer.invalid", "unrelated-project")
    before = await retained(ledger, record)
    with acting(reviewer), pytest.raises(AccessDenied):
        await ledger.invalidation_receipt(record["id"], request)
    assert await retained(ledger, record) == before


async def test_receipt_rechecks_current_credentials_without_mutating_retained_event(store, tmp_path, monkeypatch):
    ledger, record, _, reviewer = await case(store, tmp_path)
    request, reason = uuid4().hex, "owned-reclassified-credential-" + uuid4().hex
    with acting(reviewer):
        await ledger.invalidate(record["id"], reason=reason, request_id=request)
    before = await retained(ledger, record)
    monkeypatch.setenv("FORGE_TEST_API_TOKEN", reason)
    with acting(reviewer), pytest.raises(ValueError, match="credentials") as failure:
        await ledger.invalidation_receipt(record["id"], request)
    assert reason not in str(failure.value)
    assert await retained(ledger, record) == before


async def test_corrupted_repository_result_is_not_returned_as_committed_evidence(store, tmp_path, monkeypatch):
    from types import SimpleNamespace

    ledger, record, _, reviewer = await case(store, tmp_path)
    request = uuid4().hex
    with acting(reviewer):
        await ledger.invalidate(record["id"], reason="Original immutable decision", request_id=request)
    before = await retained(ledger, record)
    actual = ledger.events.for_request

    async def corrupted(record_id, request_id):
        event = await actual(record_id, request_id)
        return SimpleNamespace(
            digest=event.digest,
            sequence=event.sequence,
            data={**event.data, "payload": {"state": "invalidated", "reason": "Unbound changed content"}},
        )

    monkeypatch.setattr(ledger.events, "for_request", corrupted)
    with acting(reviewer), pytest.raises(ValueError, match="integrity"):
        await ledger.invalidation_receipt(record["id"], request)
    assert await retained(ledger, record) == before


async def test_historical_receipt_survives_rejected_unsupported_lifecycle_transition(store, tmp_path):
    from sqlalchemy.exc import DBAPIError

    ledger, record, _, reviewer = await case(store, tmp_path)
    origin_request = (await retained(ledger, record))[0][1]["request_id"]
    with acting(reviewer), pytest.raises(KeyError, match="not observed"):
        await ledger.invalidation_receipt(record["id"], origin_request)
    request = uuid4().hex
    with acting(reviewer):
        await ledger.invalidate(record["id"], reason="Historical decision", request_id=request)
    before = await retained(ledger, record)
    with pytest.raises(DBAPIError, match="state transition"):
        async with store._unit():
            row = await ledger.records_repository.find_by_id(UUID(record["id"]))
            row.state = "superseded"
            await ledger.records_repository.save(row)
    with acting(reviewer):
        receipt = await ledger.invalidation_receipt(record["id"], request)
    assert receipt["event_state"] == "invalidated" and receipt["current_record_state"] == "invalidated"
    assert receipt["digest"] == before[-1][2] and await retained(ledger, record) == before


async def test_inflight_invalidation_is_observed_only_after_its_transaction_commits(store, tmp_path, monkeypatch):
    import asyncio

    from sqlalchemy import text

    ledger, record, _, reviewer = await case(store, tmp_path)
    reader = LearningLedger(store, ledger.journal)
    appended, release, reading = asyncio.Event(), asyncio.Event(), asyncio.Event()
    actual_append, actual_find = ledger._append, reader.records_repository.find_by_id
    request, reader_pid = uuid4().hex, []

    async def hold_append(*args):
        event = await actual_append(*args)
        appended.set()
        await release.wait()
        return event

    async def observe_reader(*args, **kwargs):
        session = reader.records_repository._require_session()
        reader_pid.append(await session.scalar(text("SELECT pg_backend_pid()")))
        reading.set()
        return await actual_find(*args, **kwargs)

    monkeypatch.setattr(ledger, "_append", hold_append)
    monkeypatch.setattr(reader.records_repository, "find_by_id", observe_reader)

    async def write():
        with acting(reviewer):
            return await ledger.invalidate(record["id"], reason="Atomic reviewed decision", request_id=request)

    async def read():
        with acting(reviewer):
            return await reader.invalidation_receipt(record["id"], request)

    writer = asyncio.create_task(write())
    pending = None
    try:
        async with asyncio.timeout(10):
            await appended.wait()
            pending = asyncio.create_task(read())
            await reading.wait()
            while True:
                async with store._unit() as session:
                    blockers = await session.scalar(
                        text("SELECT cardinality(pg_blocking_pids(:pid))"), {"pid": reader_pid[0]}
                    )
                if blockers:
                    break
                await asyncio.sleep(0.01)
            assert not pending.done()
            release.set()
            await writer
            receipt = await pending
        assert receipt["request_id"] == request and receipt["reason"] == "Atomic reviewed decision"
    finally:
        release.set()
        await asyncio.gather(writer, *([pending] if pending is not None else []), return_exceptions=True)
