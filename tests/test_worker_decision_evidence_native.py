# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""PostgreSQL waiting/restart/answer/preview controls; no model or source-control call."""

import asyncio
import hashlib

import pytest
from test_worker_evidence_host import screenshot
from test_worker_message_idempotency_native import feedback, prepared

from firefly_forge.agent_roles import WorkerResponse
from firefly_forge.engine import Engine
from firefly_forge.journal import Journal
from firefly_forge.project_access import CURRENT_PRINCIPAL, AccessDenied, ProjectAccess
from firefly_forge.worker_evidence import WorkerEvidence
from firefly_forge.worker_evidence_controllers import WorkerEvidenceController


async def question_with_image(store, tmp_path):
    run, journal, actor, identity = await prepared(store, tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    screenshot(workspace / "choice.png")
    value = feedback("design-with-evidence").model_copy(update={"evidence_paths": ["choice.png"]})
    evidence = await WorkerEvidence(store, journal).freeze(run, value, workspace, workspace)
    recorded = await journal.worker_feedback(run["id"], value, evidence=evidence)
    return run, journal, actor, identity, value, evidence, recorded


async def test_waiting_question_and_exact_preview_survive_native_store_restart_without_model_call(
    store, store_factory, tmp_path, monkeypatch
):
    run, journal, actor, identity, value, evidence, recorded = await question_with_image(store, tmp_path)
    entries = await journal.entries(run["id"])
    assert Journal.verify_chain(entries)
    assert [entry["kind"] for entry in entries][-2:] == ["worker_feedback", "worker_evidence"]
    assert entries[-1]["payload"] == evidence
    path = store.path
    await store.close()
    reopened = await store_factory(path)
    restarted_journal = Journal(reopened)
    model_calls = []

    def forbidden_provider(*_args, **_kwargs):
        model_calls.append(1)
        raise AssertionError("A waiting human question must not dispatch a model")

    engine = Engine(reopened, tmp_path / "workspaces", provider_factory=forbidden_provider, journal=restarted_journal)
    polled = asyncio.Event()
    queued = reopened.queued

    async def observed_native_queue():
        result = await queued()
        polled.set()
        return result

    monkeypatch.setattr(reopened, "queued", observed_native_queue)
    try:
        await engine.start()
        await asyncio.wait_for(polled.wait(), timeout=3)
        assert model_calls == [] and not engine.active
        current = await reopened.get_run(run["id"])
        assert current["status"] == "paused" and current["worker_request"]["status"] == "pending"
        assert current["worker_request"]["id"] == value.request.id
        assert await restarted_journal.entries(run["id"]) == entries
        context = CURRENT_PRINCIPAL.set(actor)
        try:
            service = WorkerEvidence(reopened, restarted_journal)
            images = await service.list(run["id"], value.request.id)
            assert images == evidence["images"]
            result = await WorkerEvidenceController(service).image(run["id"], images[0]["id"])
            assert result.media_type == "image/png"
            assert result.headers["cache-control"] == "private, no-store"
            assert hashlib.sha256(result.body).hexdigest() == images[0]["sha256"]
        finally:
            CURRENT_PRINCIPAL.reset(context)
    finally:
        await engine.stop()
        await identity.client.aclose()


async def test_current_reader_revocation_and_cross_run_preview_are_denied(store, tmp_path):
    run, journal, actor, identity, value, evidence, _ = await question_with_image(store, tmp_path)
    service = WorkerEvidence(store, journal)
    try:
        with pytest.raises(AccessDenied):
            await service.image(run["id"], evidence["images"][0]["id"])
        token = CURRENT_PRINCIPAL.set(actor)
        try:
            assert len(await service.list(run["id"], value.request.id)) == 1
            with pytest.raises(KeyError):
                await service.image(run["id"], "0" * 64)
            await ProjectAccess(store).set_membership(run["project_id"], actor, None)
            with pytest.raises(AccessDenied):
                await service.image(run["id"], evidence["images"][0]["id"])
        finally:
            CURRENT_PRINCIPAL.reset(token)
    finally:
        await identity.client.aclose()


async def test_answer_is_actor_bound_idempotent_and_does_not_resume_or_replace_evidence(store, tmp_path):
    run, journal, actor, identity, value, evidence, _ = await question_with_image(store, tmp_path)
    response = WorkerResponse(id="human-choice", actor="Untrusted display label", answer="Keep the fictional result")
    token = CURRENT_PRINCIPAL.set(actor)
    try:
        original = await journal.worker_response(run["id"], value.request.id, response)
        entries = await journal.entries(run["id"])
        assert await journal.worker_response(run["id"], value.request.id, response) == original
        with pytest.raises(ValueError, match="different content"):
            await journal.worker_response(
                run["id"], value.request.id, response.model_copy(update={"answer": "Another choice"})
            )
        assert await journal.entries(run["id"]) == entries
        assert not any(entry["kind"] == "resume" for entry in entries)
        current = await store.get_run(run["id"])
        assert current["status"] == "paused" and current["worker_request"]["status"] == "answered"
        assert original["payload"]["responding_actor"]["user_id"] == actor.id.hex
        assert await WorkerEvidence(store, journal).list(run["id"], value.request.id) == evidence["images"]
    finally:
        CURRENT_PRINCIPAL.reset(token)
        await identity.client.aclose()


async def test_evidence_replay_and_failed_binding_do_not_change_native_question_or_wal(store, tmp_path):
    run, journal, _, identity, value, evidence, recorded = await question_with_image(store, tmp_path)
    try:
        entries = await journal.entries(run["id"])
        assert await journal.worker_feedback(run["id"], value, evidence=evidence) == recorded
        altered = {**evidence, "request_id": "different-question"}
        with pytest.raises(ValueError, match="exact native feedback"):
            await journal.worker_feedback(run["id"], value, evidence=altered)
        assert await journal.entries(run["id"]) == entries and Journal.verify_chain(entries)
        assert (await store.get_run(run["id"]))["worker_evidence"] == evidence
    finally:
        await identity.client.aclose()
