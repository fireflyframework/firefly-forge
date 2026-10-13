# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Pre-upgrade dialogue receipts keep their original context without model replay."""

import copy
import json

import pytest

from firefly_forge.assistant_persistence import ForgeAssistantTurnEntity
from firefly_forge.models import RepositoryInput
from firefly_forge.product_assistant import (
    AssistantRecoveryInput,
    AssistantSessionInput,
    AssistantTurnInput,
    ProductAssistant,
    digest,
)
from firefly_forge.store import identifier


async def test_legacy_turn_read_replay_and_recovery_preserve_original_context_without_inference(
    store, tmp_path, monkeypatch
):
    calls = []

    async def designer(prompt, settings):
        calls.append(prompt)
        raise AssertionError("A legacy receipt must never resubmit inference")

    def current_instructions(*args, **kwargs):
        raise AssertionError("Do not inject current instruction skills into a historical turn")

    monkeypatch.setattr("firefly_forge.product_assistant.assistant_instruction_snapshot", current_instructions)
    manager = ProductAssistant(store, tmp_path / "assistant", designer=designer)
    repository = await store.add_repository(RepositoryInput(name="Legacy product dialogue", url=str(tmp_path)))
    session = await manager.create(repository["id"], AssistantSessionInput())
    context = {
        "task": "product",
        "product_snapshot": {"revision": 1, "definition": {"problem": "Retain customer reservation decisions"}},
        "knowledge": {"citations": [{"id": "historical-reference", "text": "Previously retrieved product evidence"}]},
        "history": [{"message": "Who owns reservations?", "reply": {"message": "The product owner"}}],
    }
    encoded = json.dumps(context, sort_keys=True)
    request = AssistantTurnInput(request_id="d" * 32, message="Historical model request")
    turn_id = "c" * 32
    async with store._unit():
        row = await manager._record(repository["id"], session["id"], lock=True)
        await manager.turns.save(
            ForgeAssistantTurnEntity(
                id=identifier(turn_id),
                session_id=row.id,
                request_id=request.request_id,
                status="processing",
                data={
                    "id": turn_id,
                    "session_id": session["id"],
                    "request_id": request.request_id,
                    "message": request.message,
                    "status": "processing",
                    "reply": None,
                    "context": copy.deepcopy(context),
                    "created_at": "2026-10-01T00:00:00Z",
                    "updated_at": "2026-10-01T00:00:00Z",
                },
            )
        )
        await manager._event(row, "turn_intent", {"turn_id": turn_id, "context_digest": digest(context)})
        original_events = [copy.deepcopy(event.data) for event in await manager.events.events(row.id)]

    observed = await manager.session(repository["id"], session["id"])
    assert observed["turns"][0]["status"] == "processing"
    assert observed["journal"]["verified"]
    replayed = await manager.turn(repository["id"], session["id"], request)
    assert await manager.turn(repository["id"], session["id"], request) == replayed
    assert json.dumps(replayed["context"], sort_keys=True) == encoded
    recovery = AssistantRecoveryInput(request_id="e" * 32, expected_epoch=0)
    with pytest.raises(ValueError, match="confirm"):
        await manager.recover_turn(repository["id"], session["id"], turn_id, recovery)
    recovery = recovery.model_copy(update={"legacy_confirmed_stopped": True})
    recovered = await manager.recover_turn(repository["id"], session["id"], turn_id, recovery)
    assert recovered["status"] == "needs_reconciliation" and recovered["reply"] is None
    assert await manager.recover_turn(repository["id"], session["id"], turn_id, recovery) == recovered
    assert (await manager.turn(repository["id"], session["id"], request))["status"] == "needs_reconciliation"
    retained = await manager.session(repository["id"], session["id"])
    assert json.dumps(retained["turns"][0]["context"], sort_keys=True) == encoded
    assert "instruction_snapshot" not in retained["turns"][0]["context"]
    assert retained["journal"]["verified"] and calls == []
    async with store._unit():
        events = [event.data for event in await manager.events.events(identifier(session["id"]))]
    assert events[: len(original_events)] == original_events
