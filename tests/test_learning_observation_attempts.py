# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native request claims, immutable refusal proof and once-only dispatch fences."""

import asyncio
import importlib.util
import json
from uuid import uuid4

import pytest
from test_learning_invalidation_receipts import acting
from test_learning_observation_receipts import admitted_projection

from firefly_forge.identity import Principal
from firefly_forge.journal import digest
from firefly_forge.learning_observation_attempt_models import LearningObservationAttemptInput
from firefly_forge.project_access import AccessDenied, ProjectAccess


async def case(store, tmp_path, **kwargs):
    projection, record, calls = await admitted_projection(store, tmp_path, **kwargs)
    actor = Principal(uuid4(), "https://issuer.invalid", "owned-attempt-reviewer")
    await ProjectAccess(store).set_membership(record["envelope"]["context"]["project_id"], actor, "reviewer")
    status = await projection.status(record["id"])
    body = LearningObservationAttemptInput(
        attempt_id="obs2_" + uuid4().hex,
        source_id="owned-source",
        projection_id=status["id"],
        submission_request_id=status["request_id"],
        workspace_id=status["workspace_id"],
        target_revision=status["target"]["revision"],
        payload_digest=status["payload_digest"],
        content_sha256=status["content_sha256"],
    )
    return projection, record, actor, body, calls


async def observe(projection, record, body):
    if importlib.util.find_spec("firefly_forge.learning_observation_attempts") is None:
        return await projection.observe(record["id"], source_id=body.source_id, request_id=body.attempt_id[5:])
    from firefly_forge.learning_observation_attempts import LearningObservationAttempts

    return await LearningObservationAttempts(projection).observe(record["id"], body)


async def read(projection, record, body):
    from firefly_forge.learning_observation_attempts import LearningObservationAttempts

    return await LearningObservationAttempts(projection).receipt(record["id"], body.attempt_id)


@pytest.mark.parametrize("retained_source", [False, True])
async def test_credential_refusal_is_durable_exact_and_before_any_remote_io(
    store, tmp_path, monkeypatch, retained_source
):
    projection, record, actor, body, calls = await case(store, tmp_path)
    if retained_source:
        body = body.model_copy(update={"source_id": None})
    monkeypatch.setenv("FORGE_TEST_API_TOKEN", "owned-source")
    with acting(actor):
        result = await observe(projection, record, body)
        receipt = await read(projection, record, body)
    assert result == receipt and receipt["state"] == "not_dispatched"
    assert receipt["disposition"] == "local_admission_refused"
    assert receipt["input_digest"] == digest(body.model_dump())
    assert receipt["source_digest"] == digest({"source_id": body.source_id})
    assert receipt["actor_id"] == actor.id.hex and receipt["record_id"] == record["id"]
    assert receipt["digest"] == digest({key: value for key, value in receipt.items() if key != "digest"})
    assert "owned-source" not in json.dumps(receipt) and calls == []
    from sqlalchemy import select

    from firefly_forge.learning_observation_attempt_persistence import ForgeLearningObservationAttemptEntity

    async with store._unit() as session:
        row = await session.scalar(select(ForgeLearningObservationAttemptEntity))
        assert "owned-source" not in json.dumps(row.data)
    with acting(actor):
        assert await observe(projection, record, body) == receipt
    assert calls == []


async def test_verified_attempt_is_bound_to_one_exact_event_and_never_replayed(store, tmp_path):
    projection, record, actor, body, calls = await case(store, tmp_path)
    with acting(actor):
        result = await observe(projection, record, body)
        assert result["state"] == "verified"
        assert await read(projection, record, body) == result
        assert await observe(projection, record, body) == result
        event = await projection.observation_receipt(record["id"], result["observation"]["request_id"])
        assert event["digest"] == result["observation"]["digest"]
        assert event["actor_id"] == actor.id.hex and event["source_id"] == "owned-source"
        with pytest.raises(KeyError):
            await projection.observation_receipt(record["id"], body.attempt_id[5:])
    assert calls == [("GET", "/api/v1/sources/owned-source"), ("POST", "/api/v1/search")]


async def test_native_uuid_path_spelling_cannot_change_source_or_event_identity(store, tmp_path):
    from uuid import UUID

    projection, record, actor, body, calls = await case(store, tmp_path)
    with acting(actor):
        receipt = await observe(projection, {**record, "id": str(UUID(record["id"]))}, body)
        assert receipt["state"] == "verified"
        assert await read(projection, record, body) == receipt
    assert len(calls) == 2


async def test_remote_mismatch_is_unknown_and_cannot_become_local_refusal_or_replay(store, tmp_path):
    projection, record, actor, body, calls = await case(store, tmp_path, source_id="foreign-source")
    with acting(actor):
        result = await observe(projection, record, body)
        assert result["state"] == "unknown" and result["disposition"] is None
        assert await observe(projection, record, body) == result
    assert calls == [("GET", "/api/v1/sources/owned-source")]


async def test_fresh_v2_identity_cannot_bypass_a_prior_unknown_attempt(store, tmp_path):
    projection, record, actor, body, calls = await case(store, tmp_path, source_id="foreign-source")
    with acting(actor):
        original = await observe(projection, record, body)
        assert original["state"] == "unknown"
        with pytest.raises(ValueError, match="original|unresolved"):
            await observe(projection, record, body.model_copy(update={"attempt_id": "obs2_" + uuid4().hex}))
    assert calls == [("GET", "/api/v1/sources/owned-source")]


async def test_fresh_identity_after_exact_local_refusal_has_its_own_event(store, tmp_path, monkeypatch):
    projection, record, actor, body, calls = await case(store, tmp_path)
    monkeypatch.setenv("FORGE_TEST_API_TOKEN", "owned-source")
    with acting(actor):
        refusal = await observe(projection, record, body)
        assert refusal["state"] == "not_dispatched"
        assert await read(projection, record, body) == refusal
        monkeypatch.delenv("FORGE_TEST_API_TOKEN")
        fresh = body.model_copy(update={"attempt_id": "obs2_" + uuid4().hex})
        result = await observe(projection, record, fresh)
        assert result["state"] == "verified" and result["attempt_id"] != refusal["attempt_id"]
        assert await read(projection, record, body) == refusal
    assert len(calls) == 2


@pytest.mark.parametrize("phase", ["preparing", "dispatched"])
async def test_cancellation_during_wait_never_replays_or_invents_terminal_refusal(store, tmp_path, monkeypatch, phase):
    from firefly_forge.learning_observation_attempts import LearningObservationAttempts

    projection, record, actor, body, calls = await case(store, tmp_path)
    started = asyncio.Event()

    async def gate(*_args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(LearningObservationAttempts, "_prepare" if phase == "preparing" else "_read", gate)
    with acting(actor):
        task = asyncio.create_task(observe(projection, record, body))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        receipt = await read(projection, record, body)
        assert receipt["state"] == phase and receipt["disposition"] is None
        assert await observe(projection, record, body) == receipt
    assert calls == []


async def test_duplicate_concurrent_attempt_does_not_dispatch_and_changed_input_conflicts(store, tmp_path, monkeypatch):
    from firefly_forge.learning_observation_attempts import LearningObservationAttempts

    projection, record, actor, body, calls = await case(store, tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    original = LearningObservationAttempts._prepare

    async def gate(self, *args):
        value = await original(self, *args)
        started.set()
        await release.wait()
        return value

    monkeypatch.setattr(LearningObservationAttempts, "_prepare", gate)
    with acting(actor):
        task = asyncio.create_task(observe(projection, record, body))
        try:
            await asyncio.wait_for(started.wait(), 5)
            assert (await observe(projection, record, body))["state"] == "preparing"
            with pytest.raises(ValueError, match="original|unresolved"):
                await observe(projection, record, body.model_copy(update={"attempt_id": "obs2_" + uuid4().hex}))
            with pytest.raises(ValueError, match="another request"):
                await observe(projection, record, body.model_copy(update={"source_id": "different-source"}))
        finally:
            release.set()
        assert (await task)["state"] == "verified"
    assert len(calls) == 2


async def test_revoked_review_while_preparing_never_dispatches(store, tmp_path, monkeypatch):
    from firefly_forge.learning_observation_attempts import LearningObservationAttempts

    projection, record, actor, body, calls = await case(store, tmp_path)
    original = LearningObservationAttempts._prepare

    async def revoke(self, *args):
        result = await original(self, *args)
        await ProjectAccess(store).set_membership(record["envelope"]["context"]["project_id"], actor, None)
        return result

    monkeypatch.setattr(LearningObservationAttempts, "_prepare", revoke)
    with acting(actor), pytest.raises(AccessDenied):
        await observe(projection, record, body)
    assert calls == []


async def test_terminal_origin_outcome_and_delete_are_database_guarded(store, tmp_path, monkeypatch):
    from sqlalchemy import delete, update
    from sqlalchemy.exc import DBAPIError

    from firefly_forge.learning_observation_attempt_persistence import ForgeLearningObservationAttemptEntity

    projection, record, actor, body, _ = await case(store, tmp_path)
    monkeypatch.setenv("FORGE_TEST_API_TOKEN", "owned-source")
    with acting(actor):
        receipt = await observe(projection, record, body)
    for statement in [
        update(ForgeLearningObservationAttemptEntity).values(state="dispatched"),
        update(ForgeLearningObservationAttemptEntity).values(data={**receipt, "input_digest": "0" * 64}),
        delete(ForgeLearningObservationAttemptEntity),
    ]:
        with pytest.raises(DBAPIError, match="immutable|replay"):
            async with store._unit() as session:
                await session.execute(statement)
    with acting(actor):
        assert await read(projection, record, body) == receipt


async def test_late_known_credential_admission_is_rechecked_before_client_dispatch(store, tmp_path, monkeypatch):
    from firefly_forge.learning_observation_attempts import LearningObservationAttempts

    projection, record, actor, body, calls = await case(store, tmp_path)
    original = LearningObservationAttempts._prepare

    async def changed_credentials(self, *args):
        result = await original(self, *args)
        monkeypatch.setenv("FORGE_TEST_API_TOKEN", "owned-source")
        return result

    monkeypatch.setattr(LearningObservationAttempts, "_prepare", changed_credentials)
    with acting(actor):
        result = await observe(projection, record, body)
    assert result["state"] == "unknown" and calls == []


async def test_dispatch_claim_is_committed_before_client_is_opened(store, tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    from sqlalchemy import text

    projection, record, actor, body, calls = await case(store, tmp_path)
    original = projection.knowledge.client
    observed = []

    @asynccontextmanager
    async def client(*args, **kwargs):
        async with store._engine.connect() as connection:
            state = await connection.scalar(
                text(
                    f'SELECT state FROM "{store.schema}".forge_learning_observation_attempts WHERE attempt_id=:attempt'
                ),
                {"attempt": body.attempt_id},
            )
            observed.append(state)
        async with original(*args, **kwargs) as value:
            yield value

    monkeypatch.setattr(projection.knowledge, "client", client)
    with acting(actor):
        assert (await observe(projection, record, body))["state"] == "verified"
    assert observed == ["dispatched"] and len(calls) == 2


async def test_authority_revoked_during_last_credential_read_never_opens_client(store, tmp_path):
    projection, record, actor, body, calls = await case(store, tmp_path)
    selected_reads = 0

    async def admission(value):
        nonlocal selected_reads
        if value == "owned-source":
            selected_reads += 1
            if selected_reads == 2:
                await ProjectAccess(store).set_membership(record["envelope"]["context"]["project_id"], actor, None)

    store.vault_credential_admission = admission
    with acting(actor), pytest.raises(AccessDenied):
        await observe(projection, record, body)
    assert calls == []
    await ProjectAccess(store).set_membership(record["envelope"]["context"]["project_id"], actor, "reviewer")
    with acting(actor):
        assert (await read(projection, record, body))["state"] == "dispatched"


async def test_projection_drift_during_remote_read_never_confirms_another_identity(store, tmp_path):
    from sqlalchemy import update

    from firefly_forge.learning_persistence import ForgeLearningProjectionEntity

    async def drift():
        async with store._unit() as session:
            await session.execute(update(ForgeLearningProjectionEntity).values(id=uuid4()))

    projection, record, actor, body, calls = await case(store, tmp_path, before_search=drift)
    with acting(actor):
        receipt = await observe(projection, record, body)
        assert receipt["state"] == "unknown" and receipt["observation"] is None
        assert await observe(projection, record, body) == receipt
    assert len(calls) == 2


async def test_missing_legacy_foreign_and_revoked_receipts_do_not_authorize_another_attempt(store, tmp_path):
    projection, record, actor, body, calls = await case(store, tmp_path, source_id="foreign-source")
    with acting(actor), pytest.raises(KeyError):
        await read(projection, record, body)
    from firefly_forge.learning_observation_attempts import LearningObservationAttempts

    with acting(actor), pytest.raises(ValueError):
        await LearningObservationAttempts(projection).receipt(record["id"], body.attempt_id[5:])
    with acting(actor):
        receipt = await observe(projection, record, body)
    another = Principal(uuid4(), "https://issuer.invalid", "other-owned-reviewer")
    await ProjectAccess(store).set_membership(record["envelope"]["context"]["project_id"], another, "reviewer")
    with acting(another), pytest.raises(AccessDenied):
        await read(projection, record, body)
    await ProjectAccess(store).set_membership(record["envelope"]["context"]["project_id"], actor, None)
    with acting(actor), pytest.raises(AccessDenied):
        await read(projection, record, body)
    assert receipt["state"] == "unknown" and len(calls) == 1


async def test_late_review_revocation_during_receipt_admission_clears_the_read(store, tmp_path):
    projection, record, actor, body, _ = await case(store, tmp_path)
    with acting(actor):
        await observe(projection, record, body)

    async def revoke(value):
        if isinstance(value, dict) and value.get("version") == 2:
            await ProjectAccess(store).set_membership(record["envelope"]["context"]["project_id"], actor, None)

    store.vault_credential_admission = revoke
    with acting(actor), pytest.raises(AccessDenied):
        await read(projection, record, body)


async def test_local_admission_unavailability_is_not_classified_from_error_text(store, tmp_path):
    projection, record, actor, body, calls = await case(store, tmp_path)

    async def unavailable(value):
        if value == "owned-source":
            raise ValueError("Private operational details must not be reflected or classified as invalid source")

    store.vault_credential_admission = unavailable
    with acting(actor):
        receipt = await observe(projection, record, body)
    assert receipt["state"] == "not_dispatched" and receipt["disposition"] == "local_admission_refused"
    assert "Private operational" not in json.dumps(receipt) and calls == []


@pytest.mark.parametrize("phase", ["dispatched", "not_dispatched"])
async def test_lost_commit_acknowledgement_never_replays_or_invents_a_decision(store, tmp_path, monkeypatch, phase):
    from contextlib import asynccontextmanager

    from firefly_forge.learning_observation_attempts import LearningObservationAttempts

    projection, record, actor, body, calls = await case(store, tmp_path)
    if phase == "not_dispatched":
        monkeypatch.setenv("FORGE_TEST_API_TOKEN", "owned-source")
    unit = store._unit
    change = LearningObservationAttempts._change
    lose = False

    async def changed(self, row, state, **result):
        nonlocal lose
        value = await change(self, row, state, **result)
        if state == phase:
            lose = True
        return value

    @asynccontextmanager
    async def commit_unknown(**kwargs):
        nonlocal lose
        async with unit(**kwargs) as session:
            yield session
        if lose:
            lose = False
            raise RuntimeError("Owned lost commit acknowledgement")

    monkeypatch.setattr(LearningObservationAttempts, "_change", changed)
    monkeypatch.setattr(store, "_unit", commit_unknown)
    with acting(actor), pytest.raises(RuntimeError, match="commit acknowledgement"):
        await observe(projection, record, body)
    with acting(actor):
        receipt = await read(projection, record, body)
        assert receipt["state"] == phase
        assert await observe(projection, record, body) == receipt
    assert calls == []
