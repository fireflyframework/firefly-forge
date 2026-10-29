# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Command acknowledgement follows durable task phase through returned worker output."""

import copy
from uuid import uuid4

import pytest
from test_checkpoint_mode_adoption_host import request
from test_sandbox_command_ordinals_host import command_case as command_case
from test_unknown_command_acknowledgement_host import adopt_and_begin, consent, service
from test_unknown_command_acknowledgement_host import unknown_case as unknown_case

from firefly_forge.checkpoint_mode_adoptions import CheckpointModeAdoptions
from firefly_forge.checkpoint_mode_runtime import mode_adoption_scope, prepare_current_mode
from firefly_forge.configuration import ProviderConfiguration
from firefly_forge.process import ProcessResult
from firefly_forge.project_access import AccessDenied


@pytest.fixture
async def lineage(unknown_case, monkeypatch):
    import firefly_forge.unknown_command_acknowledgements as acknowledgement

    c = unknown_case
    c.store.configuration.update(
        "bridge",
        ProviderConfiguration(
            values={
                "endpoint": "https://bridge.example.test",
                "token": "synthetic-mode-lineage-key",
            }
        ),
    )
    c.store.run.data.update(agent="bridge", repair_budget=2)
    c.store.run.data.update(c.store.configuration.snapshot("bridge"))
    old_snapshot = c.store.run.data["config_revision"]
    api = service(c, monkeypatch)
    with monkeypatch.context() as old_policy:
        old_policy.setattr(acknowledgement, "policy_digest", lambda: "a" * 64)
        earlier = await api.candidate(c.run_id, c.unknown.id.hex, agent="bridge", adoption_request_id=uuid4().hex)
        earlier_ack = await api.request(c.run_id, c.unknown.id.hex, uuid4().hex, consent(earlier))
        reviewed = await api.candidate(c.run_id, c.unknown.id.hex, agent="bridge", adoption_request_id=uuid4().hex)
        original_ack = await api.request(c.run_id, c.unknown.id.hex, uuid4().hex, consent(reviewed))
        assert earlier_ack["payload"]["actor"] == original_ack["payload"]["actor"]
        assert (
            earlier_ack["payload"]["candidate"]["policy_digest"]
            == original_ack["payload"]["candidate"]["policy_digest"]
        )
        intent = await adopt_and_begin(c, reviewed, monkeypatch)

        async def known_red(config, argv, *args):
            return ProcessResult(argv, 1, "Known controlled RED")

        c.backend.run = known_red
        await c.fresh().execute(c.run_id, c.workspace, c.workspace, ["fixture-check"], 2, "implement", ordinal=3)
        await c.journal.verification_command(c.run_id, intent, 0, check_current=lambda _: None)
        await c.journal.verification_finish(c.run_id, intent, check_current=lambda _: None)
        await c.journal._append(
            c.run_id,
            "handoff",
            uuid4().hex,
            "handoff",
            {
                "automatic": True,
                "verification_handoff_id": intent["id"],
                "agent": "bridge",
                "config_revision": old_snapshot,
            },
        )
        task = c.store.tasks.rows[0]
        task.status, task.attempt = "queued", 2
        c.store.run.data.update(attempt=2, implementation_pending=True)

        async def plan(*args):
            return {}

        monkeypatch.setattr(c.journal, "ensure_plan", plan)
        await c.journal.intent(c.run_id, "implement", 2, {"config_revision": old_snapshot})
        await c.journal.verification_provider_admission(c.run_id)
    c.store.run.data.update(status="failed", error="native_agentic_checkpoint_wal_required", active_task="implement")
    c.store.run.status = "failed"
    c.engine.active = {}
    c.engine.tool_broker = None
    c.store.configuration.update(
        "bridge", ProviderConfiguration(values={"checkpoints": "required", "completion_recovery": "required"})
    )
    adoption = CheckpointModeAdoptions(c.store, c.journal, c.engine)
    candidate = await adoption.candidate(c.run_id)
    assert len(candidate["acknowledgements"]) == 1
    assert candidate["acknowledgements"][0]["record_digest"] == original_ack["digest"]
    await adoption.request(c.run_id, uuid4().hex, request(candidate))
    prepared = await prepare_current_mode(c.journal, c.run_id)
    with mode_adoption_scope(prepared):
        await c.journal.intent(c.run_id, "implement", 2)
        await c.journal.verification_provider_admission(c.run_id)
    return c, adoption, prepared, original_ack


@pytest.mark.parametrize("phase", ["model_returned", "receipt_committed"])
async def test_existing_ack_survives_model_return_and_receipt_without_new_command_or_grant(lineage, phase):
    c, adoption, prepared, ack = lineage
    task = c.store.tasks.rows[0]
    before_unknown = copy.deepcopy((c.unknown.data, c.unknown.status))
    before_calls = len(c.calls)
    with mode_adoption_scope(prepared):
        if phase == "receipt_committed":
            await c.journal.receipt(c.run_id, task.data["idempotency_key"], {"implementation_round": 2})
        c.store.run.data.update(implementation_pending=False, implementation_rounds=2)
        entries = await c.journal.entries(c.run_id)
        result, _ = await adoption._command_custody(await c.store.get_run(c.run_id), {"attempt": 2}, entries)
    assert len(result) == 1 and result[0]["record_digest"] == ack["digest"]
    assert (c.unknown.data, c.unknown.status) == before_unknown
    assert len(c.calls) == before_calls
    assert c.store.run.data["implementation_pending"] is False
    assert c.store.run.data["implementation_rounds"] == 2


@pytest.mark.parametrize("change", ["round", "attempt", "ack", "intent", "permission"])
async def test_phase_projection_does_not_replace_missing_lineage_or_current_authority(lineage, change):
    c, adoption, prepared, ack = lineage
    c.store.run.data.update(implementation_pending=False, implementation_rounds=2)
    entries = await c.journal.entries(c.run_id)
    if change == "round":
        c.store.run.data["implementation_rounds"] = 17
    elif change == "attempt":
        c.store.run.data["attempt"] = 3
    elif change == "ack":
        entries = [e for e in entries if e["digest"] != ack["digest"]]
    elif change == "intent":
        entries = [
            e
            for e in entries
            if not (e["kind"] == "intent" and e["idempotency_key"] == c.store.tasks.rows[0].data["idempotency_key"])
        ]
    elif change == "permission":
        c.allowed = False
    with mode_adoption_scope(prepared), pytest.raises((ValueError, AccessDenied)):
        await adoption._command_custody(await c.store.get_run(c.run_id), {"attempt": 2}, entries)


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_intent",
        "duplicate_receipt",
        "receipt_round",
        "receipt_task",
        "receipt_order",
        "receipt_without_done",
        "task_key",
        "task_attempt",
        "duplicate_ack",
        "round_bool",
        "pending_int",
    ],
)
async def test_durable_phase_rejects_ambiguous_or_foreign_native_records(lineage, change):
    c, adoption, prepared, ack = lineage
    task = c.store.tasks.rows[0]
    with mode_adoption_scope(prepared):
        if change.startswith("receipt") or change == "duplicate_receipt":
            await c.journal.receipt(c.run_id, task.data["idempotency_key"], {"implementation_round": 2})
        c.store.run.data.update(implementation_pending=False, implementation_rounds=2)
        entries = await c.journal.entries(c.run_id)
        if change == "duplicate_intent":
            entries.append(
                copy.deepcopy(
                    next(
                        e
                        for e in entries
                        if e["kind"] == "intent" and e["idempotency_key"] == task.data["idempotency_key"]
                    )
                )
            )
        elif change == "duplicate_ack":
            entries.append(copy.deepcopy(ack))
        elif change == "task_key":
            task.data["idempotency_key"] = uuid4().hex
        elif change == "task_attempt":
            task.attempt = 3
        elif change == "round_bool":
            c.store.run.data["implementation_rounds"] = True
        elif change == "pending_int":
            c.store.run.data["implementation_pending"] = 0
        else:
            receipt = next(
                e for e in entries if e["kind"] == "receipt" and e["idempotency_key"] == task.data["idempotency_key"]
            )
            if change == "duplicate_receipt":
                entries.append(copy.deepcopy(receipt))
            elif change == "receipt_round":
                receipt["payload"]["implementation_round"] = 3
            elif change == "receipt_task":
                receipt["task_id"] = "verify"
            elif change == "receipt_order":
                receipt["sequence"] = 0
            elif change == "receipt_without_done":
                task.status = "running"
        with pytest.raises(ValueError):
            await adoption._command_custody(await c.store.get_run(c.run_id), {"attempt": 2}, entries)
