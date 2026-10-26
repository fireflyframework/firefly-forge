# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Strong-mode recovery preserves frozen settings, original task and spent history."""

import copy
from uuid import NAMESPACE_URL, uuid5

import pytest

from firefly_forge.journal import digest


def test_mode_events_fit_the_existing_native_journal_kind_column():
    from firefly_forge.checkpoint_mode_proof import ADOPTION, CLAIM
    from firefly_forge.persistence import ForgeJournalEntity

    width = ForgeJournalEntity.__table__.c.kind.type.length
    assert all(len(kind) <= width for kind in (ADOPTION, CLAIM, "checkpoint_mode_intent"))


def case():
    settings = {
        "FORGE_BRIDGE_MODEL": "controlled/model",
        "FORGE_BRIDGE_URL": "https://example.test/v1",
        "FORGE_BRIDGE_TOKEN": "synthetic-not-a-credential",
        "FORGE_PROVIDER_TIMEOUT": "1800",
    }
    run = {
        "id": "1" * 32,
        "agent": "bridge",
        "status": "failed",
        "error": "native_agentic_checkpoint_wal_required",
        "active_task": "implement",
        "attempt": 2,
        "implementation_rounds": 1,
        "implementation_pending": True,
        "config_revision": "2" * 32,
        "repair_budget": 2,
        "commands": [{"argv": ["controlled-check"], "timeout": 1}],
        "verification_handoff": {"id": "3" * 32, "status": "completed"},
        "worker_response": {"source": "factory_verification", "verification_handoff_id": "3" * 32},
        "sandbox_snapshot": {"digest": "a" * 64},
        "tool_policy_snapshot": {"digest": "b" * 64},
    }
    task = {
        "id": "implement",
        "status": "running",
        "attempt": 2,
        "idempotency_key": uuid5(NAMESPACE_URL, "forge:" + run["id"] + ":implement:2").hex,
    }
    entries = []

    def append(kind, key, payload):
        value = {
            "run_id": run["id"],
            "sequence": len(entries) + 1,
            "task_id": "implement",
            "idempotency_key": key,
            "kind": kind,
            "payload": payload,
            "previous_hash": entries[-1]["digest"] if entries else "0" * 64,
        }
        value["digest"] = digest(value)
        entries.append(value)

    append("intent", task["idempotency_key"], {"config_revision": run["config_revision"]})
    append(
        "verification_continuation_intent",
        run["verification_handoff"]["id"] + ":coding",
        {
            "id": run["verification_handoff"]["id"],
            "parent_round": 1,
            "next_round": 2,
            "provider_digest": digest({"agent": "bridge", "settings": settings}),
        },
    )
    return run, task, entries, settings


def test_exact_predispatch_failure_retains_both_intent_digests_and_all_settings():
    from firefly_forge.checkpoint_mode_proof import prepare_adoption, required_settings

    run, task, entries, settings = case()
    before = copy.deepcopy((run, task, entries, settings))
    proof = prepare_adoption(run, task, entries, settings)
    assert proof["implementation_intent_digest"] == entries[-2]["digest"]
    assert proof["continuation_intent_digest"] == entries[-1]["digest"]
    assert proof["original_config_revision"] == run["config_revision"]
    assert proof["task_key"] == task["idempotency_key"] and proof["attempt"] == 2
    assert required_settings(settings) == settings | {
        "FORGE_AGENTIC_CHECKPOINTS": "required",
        "FORGE_BRIDGE_COMPLETION_RECOVERY": "required",
    }
    assert (run, task, entries, settings) == before


@pytest.mark.parametrize(
    "change",
    [
        "error",
        "status",
        "agent",
        "task",
        "attempt",
        "completed",
        "cancel",
        "bridge_hold",
        "model_dispatch",
        "provider_intent",
        "malformed_chain",
        "changed_provider",
        "unknown_old_completion",
    ],
)
def test_only_exact_known_before_dispatch_failure_can_be_adopted(change):
    from firefly_forge.checkpoint_mode_proof import prepare_adoption

    run, task, entries, settings = case()
    if change == "error":
        run["error"] = "unknown provider outcome"
    elif change == "status":
        run["status"] = "developing"
    elif change == "agent":
        run["agent"] = "agentic"
    elif change == "task":
        task["status"] = "done"
    elif change == "attempt":
        task["attempt"] = 3
    elif change == "completed":
        run["implementation_rounds"] = 2
    elif change == "cancel":
        run["cancel_request"] = {"status": "requested"}
    elif change == "bridge_hold":
        run["bridge_completion_hold"] = {"reason": "unknown"}
    elif change == "changed_provider":
        settings["FORGE_BRIDGE_MODEL"] = "different/model"
    elif change == "unknown_old_completion":
        entries.insert(0, {**entries[0], "kind": "bridge_request_intent", "idempotency_key": "fbr1_" + "5" * 32})
    elif change == "malformed_chain":
        entries[-1]["digest"] = "c" * 64
    else:
        entries[-1]["kind"] = "bridge_request_intent" if change == "model_dispatch" else "provider_dispatch_intent"
    if change != "malformed_chain":
        previous = "0" * 64
        for number, entry in enumerate(entries, 1):
            entry["sequence"] = number
            entry["previous_hash"] = previous
            entry["digest"] = digest({k: v for k, v in entry.items() if k != "digest"})
            previous = entry["digest"]
    with pytest.raises(ValueError):
        prepare_adoption(run, task, entries, settings)


def test_existing_required_modes_cannot_rearm_a_consumed_dispatch():
    from firefly_forge.checkpoint_mode_proof import prepare_adoption, required_settings

    run, task, entries, settings = case()
    with pytest.raises(ValueError):
        prepare_adoption(run, task, entries, required_settings(settings))


def recovered_case():
    run, task, entries, settings = case()
    task["status"] = "needs_reconciliation"
    record = {
        "run_id": run["id"],
        "sequence": len(entries) + 1,
        "task_id": "implement",
        "idempotency_key": task["idempotency_key"],
        "kind": "recovery",
        "payload": {
            "reason": "Worker stopped without a durable receipt; inspect the retained source and external target"
        },
        "previous_hash": entries[-1]["digest"],
    }
    record["digest"] = digest(record)
    entries.append(record)
    return run, task, entries, settings


def test_one_exact_native_startup_recovery_annotation_preserves_failed_task_scope():
    from firefly_forge.checkpoint_mode_proof import prepare_adoption

    run, task, entries, settings = recovered_case()
    proof = prepare_adoption(run, task, entries, settings)
    assert proof["startup_recovery_digest"] == entries[-1]["digest"]
    assert proof["continuation_intent_digest"] == entries[-2]["digest"]
    assert task["status"] == "needs_reconciliation" and run["status"] == "failed"


@pytest.mark.parametrize("change", ["missing", "other_key", "other_task", "reason", "duplicate"])
def test_recovery_annotation_is_not_blanket_permission_for_unfinished_tasks(change):
    from firefly_forge.checkpoint_mode_proof import prepare_adoption

    run, task, entries, settings = recovered_case()
    if change == "missing":
        entries.pop()
    elif change == "other_key":
        entries[-1]["idempotency_key"] = "a" * 32
    elif change == "other_task":
        entries[-1]["task_id"] = "verify"
    elif change == "reason":
        entries[-1]["payload"]["reason"] = "operator guessed no model ran"
    else:
        entries.append(copy.deepcopy(entries[-1]))
    previous = "0" * 64
    for number, entry in enumerate(entries, 1):
        entry.update(sequence=number, previous_hash=previous)
        entry["digest"] = digest({k: v for k, v in entry.items() if k != "digest"})
        previous = entry["digest"]
    with pytest.raises(ValueError):
        prepare_adoption(run, task, entries, settings)


def test_noncanonical_task_identity_cannot_be_adopted_even_with_a_consistent_chain():
    from firefly_forge.checkpoint_mode_proof import prepare_adoption

    run, task, entries, settings = case()
    task["idempotency_key"] = "a" * 32
    entries[-2]["idempotency_key"] = "a" * 32
    previous = "0" * 64
    for entry in entries:
        entry["previous_hash"] = previous
        entry["digest"] = digest({k: v for k, v in entry.items() if k != "digest"})
        previous = entry["digest"]
    with pytest.raises(ValueError):
        prepare_adoption(run, task, entries, settings)
