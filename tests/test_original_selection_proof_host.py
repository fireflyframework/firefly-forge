# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Pure native-lineage controls; all settings and identities are synthetic."""

import copy
from uuid import NAMESPACE_URL, uuid5

import pytest

from firefly_forge.configuration import public_settings
from firefly_forge.journal import digest
from firefly_forge.original_selection_proof import prepare_selection_readmission


def case():
    blank = {
        "FORGE_BRIDGE_MODEL": "controlled/model",
        "FORGE_BRIDGE_URL": "",
        "FORGE_BRIDGE_TOKEN": "",
        "FORGE_PROVIDER_TIMEOUT": "1800",
    }
    effective = blank | {
        "FORGE_AGENTIC_CHECKPOINTS": "required",
        "FORGE_BRIDGE_COMPLETION_RECOVERY": "required",
    }
    selected = effective | {
        "FORGE_BRIDGE_URL": "https://bridge.example.test",
        "FORGE_BRIDGE_TOKEN": "synthetic-token",
    }
    routing = {"revision": 2, "bindings": {"communication": "communicator"}}
    routing["digest"] = digest(routing)
    routing.update(tasks=[], mode="explicit_task_profiles")
    selection = {
        "task": "implementation",
        "provider": "bridge",
        "profile_id": "maintenance",
        "profile_revision": 2,
        "profile_digest": digest({"provider": "bridge", "settings": selected}),
        "routing_revision": 2,
        "routing_digest": routing["digest"],
        "config_revision": "3" * 32,
        "execution_config": public_settings("bridge", selected),
        "image_input": {"status": "unverified"},
    }
    profile = {
        "name": "Maintenance",
        "description": "Controlled",
        "provider": "bridge",
        "revision": 2,
        "settings": selected,
    }
    run = {
        "id": "1" * 32,
        "agent": "bridge",
        "status": "failed",
        "error": "Configure FORGE_BRIDGE_URL and FORGE_BRIDGE_TOKEN before selecting the bridge",
        "active_task": "implement",
        "attempt": 2,
        "implementation_rounds": 1,
        "implementation_pending": True,
        "config_revision": "2" * 32,
        "model_selection": None,
        "repair_budget": 2,
        "commands": [],
        "tool_policy_snapshot": {},
        "sandbox_snapshot": {},
        "verification_handoff": {"id": "4" * 32, "status": "completed"},
    }
    task = {
        "id": "implement",
        "status": "running",
        "attempt": 2,
        "idempotency_key": uuid5(NAMESPACE_URL, "forge:" + run["id"] + ":implement:2").hex,
    }
    entries = []

    def append(kind, key, payload, task_id="implement"):
        value = {
            "run_id": run["id"],
            "sequence": len(entries) + 1,
            "task_id": task_id,
            "idempotency_key": key,
            "kind": kind,
            "payload": payload,
            "previous_hash": entries[-1]["digest"] if entries else "0" * 64,
        }
        value["digest"] = digest(value)
        entries.append(value)
        return value

    append(
        "handoff",
        "historical-handoff",
        {
            "agent": "bridge",
            "previous_agent": "bridge",
            "previous_config_revision": selection["config_revision"],
            "config_revision": run["config_revision"],
            "previous_model_selection": selection,
            "model_selection": None,
            "selection_mode": "legacy_provider",
            "automatic": True,
            "verification_handoff_id": run["verification_handoff"]["id"],
        },
        "handoff",
    )
    implement = append("intent", task["idempotency_key"], {"config_revision": run["config_revision"]})
    continuation = append(
        "verification_continuation_intent",
        run["verification_handoff"]["id"] + ":coding",
        {
            "id": run["verification_handoff"]["id"],
            "parent_round": 1,
            "next_round": 2,
            "provider_digest": digest({"agent": "bridge", "settings": blank}),
        },
    )
    proof = {
        "version": 1,
        "run_id": run["id"],
        "task_key": task["idempotency_key"],
        "attempt": 2,
        "original_config_revision": run["config_revision"],
        "original_settings_digest": digest(blank),
        "historical_custody_digest": "a" * 64,
        "required_settings_digest": digest(effective),
        "implementation_intent_digest": implement["digest"],
        "continuation_intent_digest": continuation["digest"],
        "continuation_key": continuation["idempotency_key"],
        "provider_digest": digest({"agent": "bridge", "settings": blank}),
        "journal_digest": entries[-1]["digest"],
        "original_failure": "native_agentic_checkpoint_wal_required",
        "frozen_budget_digest": digest(
            {
                k: run.get(k)
                for k in [
                    "repair_budget",
                    "tool_policy_snapshot",
                    "sandbox_snapshot",
                    "commands",
                ]
            }
        ),
        "startup_recovery_digest": None,
        "original_task": copy.deepcopy(task),
    }
    proof["digest"] = digest(proof)
    actor = {
        "user_id": "5" * 32,
        "authentication_method": "oidc_session",
        "global_admin": True,
        "api_audience": None,
        "identity_digest": "b" * 64,
    }
    candidate = {
        "version": 1,
        "run_id": run["id"],
        "actor": actor,
        "policy_digest": "c" * 64,
        "acknowledgement_policy_digest": "d" * 64,
        "run_digest": "e" * 64,
        "source_digest": "f" * 64,
        "verification_custody_digest": "a" * 64,
        "original_scope_digest": "b" * 64,
        "proof": proof,
        "acknowledgements": [],
    }
    candidate["digest"] = digest(candidate)
    flags = [
        "acknowledge_known_pre_dispatch_failure",
        "acknowledge_no_external_model_dispatch",
        "acknowledge_original_intents_not_completed",
        "acknowledge_only_stronger_modes",
        "acknowledge_spent_history_and_budgets_preserved",
        "acknowledge_same_native_continuation",
        "acknowledge_existing_unknown_command_risk",
    ]
    request = {
        "version": 1,
        "candidate_digest": candidate["digest"],
        "policy_digest": candidate["policy_digest"],
        "operator_observation_digest": "c" * 64,
        "observation_provenance": "native_operator_pre_dispatch_observation",
        **dict.fromkeys(flags, True),
    }
    request_id = "6" * 32
    intent = append(
        "checkpoint_mode_intent",
        "checkpoint-mode-intent:" + request_id,
        {"candidate_digest": candidate["digest"], "request": request, "actor": actor},
    )
    receipt = {
        "version": 1,
        "run_id": run["id"],
        "request_id": request_id,
        "candidate": candidate,
        "request": request,
        "adoption_intent_digest": intent["digest"],
        "execution_config_revision": "7" * 32,
    }
    adoption = append("checkpoint_mode_adoption", "checkpoint-mode:" + request_id, receipt)
    claim = append(
        "checkpoint_mode_claim",
        "checkpoint-mode-claim:" + request_id,
        {
            "version": 1,
            "adoption_digest": adoption["digest"],
            "implementation_intent_digest": implement["digest"],
            "continuation_intent_digest": continuation["digest"],
            "task_key": task["idempotency_key"],
            "attempt": 2,
        },
    )
    run["checkpoint_mode_adoption"] = {
        "request_id": request_id,
        "record_digest": adoption["digest"],
        "original_config_revision": run["config_revision"],
        "execution_config_revision": receipt["execution_config_revision"],
        "policy_digest": candidate["policy_digest"],
    }
    task["checkpoint_mode_claim_digest"] = claim["digest"]
    return {
        "run": run,
        "task": task,
        "entries": entries,
        "original_settings": blank,
        "ineffective_settings": effective,
        "current_selection": selection,
        "current_profile": profile,
        "current_routing": routing,
        "current_settings": selected,
        "mode_receipt": receipt,
    }


def test_exact_original_explicit_override_and_all_inputs_are_immutable():
    c = case()
    before = copy.deepcopy(c)
    p = prepare_selection_readmission(**c)
    assert p["selection_digest"] == digest(c["current_selection"])
    assert p["journal_digest"] == c["entries"][-1]["digest"]
    assert "synthetic-token" not in str(p) and "bridge.example.test" not in str(p)
    assert c == before


@pytest.mark.parametrize(
    "group,key,value",
    [
        ("run", "status", "paused"),
        ("run", "error", "unknown readiness failure"),
        ("run", "agent", "codex"),
        ("run", "attempt", 3),
        ("run", "implementation_pending", False),
        ("run", "implementation_rounds", 2),
        ("run", "model_selection", {}),
        ("run", "config_revision", "9" * 32),
        ("run", "checkpoint_mode_adoption", {}),
        ("task", "attempt", 3),
        ("task", "status", "done"),
        ("task", "idempotency_key", "8" * 32),
        ("task", "checkpoint_mode_claim_digest", "a" * 64),
        ("current_selection", "profile_revision", 3),
        ("current_selection", "profile_id", "other"),
        ("current_selection", "config_revision", "9" * 32),
        ("current_selection", "routing_digest", "a" * 64),
        ("current_selection", "execution_config", {}),
        ("current_profile", "revision", 3),
        ("current_profile", "provider", "codex"),
        ("current_routing", "revision", 3),
        ("current_routing", "digest", "a" * 64),
        ("current_settings", "FORGE_BRIDGE_TOKEN", "changed-token"),
        ("current_settings", "FORGE_BRIDGE_MODEL", "other"),
        ("current_settings", "FORGE_PROVIDER_TIMEOUT", "7200"),
        ("current_settings", "FORGE_AGENTIC_CHECKPOINTS", "disabled"),
        ("ineffective_settings", "FORGE_BRIDGE_TOKEN", "unrelated-token"),
        ("original_settings", "FORGE_BRIDGE_URL", "https://unexpected.test"),
        ("run", "repair_budget", 3),
        ("run", "cancel_request", {"status": "requested"}),
    ],
)
def test_changed_current_or_historical_cuts_refuse(group, key, value):
    c = case()
    c[group][key] = value
    with pytest.raises(ValueError):
        prepare_selection_readmission(**c)


@pytest.mark.parametrize("kind", ["bridge_request_intent", "sandbox_command_intent", "checkpoint_mode_claim"])
def test_any_later_native_dispatch_or_repeated_claim_refuses(kind):
    c = case()
    e = copy.deepcopy(c["entries"][-1])
    e.update(sequence=7, kind=kind, previous_hash=c["entries"][-1]["digest"])
    e["digest"] = digest({k: v for k, v in e.items() if k != "digest"})
    c["entries"].append(e)
    with pytest.raises(ValueError):
        prepare_selection_readmission(**c)


@pytest.mark.parametrize(
    "key,value",
    [
        ("automatic", False),
        ("selection_mode", "named_profile"),
        ("previous_agent", "codex"),
        ("previous_model_selection", {}),
        ("model_selection", {}),
        ("verification_handoff_id", "9" * 32),
    ],
)
def test_rehashed_wrong_loss_handoff_refuses(key, value):
    c = case()
    c["entries"][0]["payload"][key] = value
    for i, e in enumerate(c["entries"]):
        e["previous_hash"] = c["entries"][i - 1]["digest"] if i else "0" * 64
        e["digest"] = digest({k: v for k, v in e.items() if k != "digest"})
    with pytest.raises(ValueError):
        prepare_selection_readmission(**c)


def recover(c, *, key=None, reason=None):
    e = {
        "run_id": c["run"]["id"],
        "sequence": len(c["entries"]) + 1,
        "task_id": "implement",
        "idempotency_key": key or c["task"]["idempotency_key"],
        "kind": "recovery",
        "payload": {
            "reason": reason
            or "Worker stopped without a durable receipt; inspect the retained source and external target"
        },
        "previous_hash": c["entries"][-1]["digest"],
    }
    e["digest"] = digest(e)
    c["entries"].append(e)
    c["task"]["status"] = "needs_reconciliation"


def test_only_exact_native_startup_recovery_can_follow_old_claim():
    c = case()
    recover(c)
    p = prepare_selection_readmission(**c)
    assert p["startup_recovery_digest"] == c["entries"][-1]["digest"]
    assert p["mode_claim_digest"] == c["entries"][-2]["digest"]


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_key",
        "wrong_reason",
        "duplicate",
        "running_with_recovery",
        "recon_without_recovery",
        "mixed_later_event",
        "wrong_task",
    ],
)
def test_startup_recovery_must_be_unique_exact_and_match_task_state(mutation):
    c = case()
    if mutation == "recon_without_recovery":
        c["task"]["status"] = "needs_reconciliation"
    else:
        recover(
            c,
            key="9" * 32 if mutation == "wrong_key" else None,
            reason="another recovery" if mutation == "wrong_reason" else None,
        )
        if mutation == "duplicate":
            recover(c)
        elif mutation == "running_with_recovery":
            c["task"]["status"] = "running"
        elif mutation in ["mixed_later_event", "wrong_task"]:
            e = copy.deepcopy(c["entries"][-1])
            if mutation == "mixed_later_event":
                e.update(
                    kind="sandbox_command_intent",
                    sequence=e["sequence"] + 1,
                    previous_hash=e["digest"],
                )
                c["entries"].append(e)
            else:
                e["task_id"] = "verify"
                c["entries"][-1] = e
            e["digest"] = digest({k: v for k, v in e.items() if k != "digest"})
    with pytest.raises(ValueError):
        prepare_selection_readmission(**c)
