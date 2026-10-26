# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Pure proof of lost native Bridge selection; no credentials leave the hash cut.

Closed Bridge history, completed verification custody and current native authority
remain separate required validators at the caller's owned admission boundary.
"""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from firefly_forge.checkpoint_mode_models import CheckpointModeReceiptV1
from firefly_forge.checkpoint_mode_proof import required_settings
from firefly_forge.configuration import public_settings
from firefly_forge.journal import Journal, digest, valid_recovery_annotation

Data = dict[str, Any]
READINESS_ERROR = "Configure FORGE_BRIDGE_URL and FORGE_BRIDGE_TOKEN before selecting the bridge"
CONNECTION = frozenset({"FORGE_BRIDGE_URL", "FORGE_BRIDGE_TOKEN"})


def prepare_selection_readmission(
    run: Data,
    task: Data,
    entries: list[Data],
    *,
    original_settings: dict[str, str],
    ineffective_settings: dict[str, str],
    current_selection: Data,
    current_profile: Data,
    current_routing: Data,
    current_settings: dict[str, str],
    mode_receipt: Data,
) -> Data:
    if not entries or not Journal.verify_chain(entries) or any(e["run_id"] != run["id"] for e in entries):
        raise ValueError("Original selection readmission requires the exact intact native journal")
    if (
        run.get("agent") != "bridge"
        or run.get("status") != "failed"
        or run.get("error") != READINESS_ERROR
        or run.get("model_selection") is not None
        or run.get("active_task") != "implement"
        or run.get("implementation_pending") is not True
        or type(run.get("attempt")) is not int
        or type(run.get("implementation_rounds")) is not int
        or run["attempt"] != run["implementation_rounds"] + 1
        or task.get("id") != "implement"
        or task.get("status") not in {"running", "needs_reconciliation"}
        or type(task.get("attempt")) is not int
        or task["attempt"] != run["attempt"]
        or (run.get("cancel_request") or {}).get("status") == "requested"
        or run.get("bridge_completion_hold")
    ):
        raise ValueError("Only the known same-attempt Bridge readiness refusal is eligible")
    key = uuid5(NAMESPACE_URL, "forge:" + run["id"] + ":implement:" + str(task["attempt"])).hex
    receipt = CheckpointModeReceiptV1.model_validate(mode_receipt)
    candidate, proof = receipt.candidate, receipt.candidate.proof
    if (
        task.get("idempotency_key") != key
        or proof.run_id != run["id"]
        or proof.task_key != key
        or proof.attempt != task["attempt"]
        or proof.original_config_revision != run.get("config_revision")
        or proof.original_settings_digest != digest(original_settings)
        or proof.required_settings_digest != digest(ineffective_settings)
        or ineffective_settings != required_settings(original_settings)
        or any(original_settings.get(k) for k in CONNECTION)
        or proof.frozen_budget_digest
        != digest(
            {
                k: run.get(k)
                for k in (
                    "repair_budget",
                    "tool_policy_snapshot",
                    "sandbox_snapshot",
                    "commands",
                )
            }
        )
    ):
        raise ValueError("The original mode/task/settings/budget cut changed")
    records = [e for e in entries if e["kind"] == "checkpoint_mode_adoption" and e["payload"] == mode_receipt]
    if len(records) != 1:
        raise ValueError("The exact typed mode receipt is unavailable or duplicated")
    record = records[0]
    reference = {
        "request_id": receipt.request_id,
        "record_digest": record["digest"],
        "original_config_revision": proof.original_config_revision,
        "execution_config_revision": receipt.execution_config_revision,
        "policy_digest": candidate.policy_digest,
    }
    expected_intent = {
        "candidate_digest": candidate.digest,
        "request": receipt.request.model_dump(mode="json"),
        "actor": candidate.actor.model_dump(mode="json"),
    }
    expected_claim = {
        "version": 1,
        "adoption_digest": record["digest"],
        "implementation_intent_digest": proof.implementation_intent_digest,
        "continuation_intent_digest": proof.continuation_intent_digest,
        "task_key": key,
        "attempt": task["attempt"],
    }
    tail = [e for e in entries if e["sequence"] > record["sequence"] - 2]
    recovery = None
    if task["status"] == "needs_reconciliation":
        if (
            len(tail) != 4
            or tail[-1]["kind"] != "recovery"
            or tail[-1]["task_id"] != "implement"
            or not valid_recovery_annotation(tail[-1], entries, "implement", key)
        ):
            raise ValueError("Only one exact native startup annotation may follow the old mode claim")
        recovery = tail[-1]
        tail = tail[:-1]
    if (
        run.get("checkpoint_mode_adoption") != reference
        or len(tail) != 3
        or tail[0]["kind"] != "checkpoint_mode_intent"
        or tail[0]["digest"] != receipt.adoption_intent_digest
        or tail[0]["idempotency_key"] != "checkpoint-mode-intent:" + receipt.request_id
        or tail[0]["previous_hash"] != proof.journal_digest
        or tail[0]["payload"] != expected_intent
        or tail[1] != record
        or record["idempotency_key"] != "checkpoint-mode:" + receipt.request_id
        or tail[2]["kind"] != "checkpoint_mode_claim"
        or tail[2]["idempotency_key"] != "checkpoint-mode-claim:" + receipt.request_id
        or tail[2]["task_id"] != "implement"
        or tail[2]["payload"] != expected_claim
        or task.get("checkpoint_mode_claim_digest") != tail[2]["digest"]
    ):
        raise ValueError("The exact mode intent/receipt/claim must be the final native evidence")
    implementation = [e for e in entries if e["digest"] == proof.implementation_intent_digest]
    continuation = [e for e in entries if e["digest"] == proof.continuation_intent_digest]
    if (
        len(implementation) != 1
        or len(continuation) != 1
        or implementation[0]["kind"] != "intent"
        or implementation[0]["task_id"] != "implement"
        or implementation[0]["idempotency_key"] != key
        or implementation[0]["payload"].get("config_revision") != run["config_revision"]
        or continuation[0]["kind"] != "verification_continuation_intent"
        or continuation[0]["sequence"] != implementation[0]["sequence"] + 1
        or continuation[0]["idempotency_key"] != proof.continuation_key
    ):
        raise ValueError("The original unfinished implementation/factory continuation changed")
    handoffs = [e for e in entries if e["sequence"] == implementation[0]["sequence"] - 1 and e["kind"] == "handoff"]
    if len(handoffs) != 1:
        raise ValueError("The exact prior native provider handoff is missing")
    handoff = handoffs[0]
    payload = handoff["payload"]
    if (
        payload.get("automatic") is not True
        or payload.get("selection_mode") != "legacy_provider"
        or payload.get("agent") != "bridge"
        or payload.get("previous_agent") != "bridge"
        or payload.get("model_selection") is not None
        or payload.get("previous_model_selection") != current_selection
        or payload.get("previous_config_revision") != current_selection.get("config_revision")
        or payload.get("config_revision") != run["config_revision"]
        or payload.get("verification_handoff_id") != (run.get("verification_handoff") or {}).get("id")
        or continuation[0]["payload"].get("id") != payload.get("verification_handoff_id")
        or continuation[0]["payload"].get("parent_round") != run["implementation_rounds"]
        or continuation[0]["payload"].get("next_round") != task["attempt"]
        or continuation[0]["payload"].get("provider_digest") != proof.provider_digest
        or proof.provider_digest != digest({"agent": "bridge", "settings": original_settings})
    ):
        raise ValueError("The native handoff did not lose this exact original selection")
    routing_cut = {k: current_routing.get(k) for k in ("revision", "bindings")}
    if (
        current_selection.get("provider") != "bridge"
        or current_selection.get("task") != "implementation"
        or current_selection.get("config_revision") == run["config_revision"]
        or current_selection.get("profile_revision") != current_profile.get("revision")
        or current_profile.get("provider") != "bridge"
        or current_profile.get("settings") != current_settings
        or current_selection.get("profile_digest") != digest({"provider": "bridge", "settings": current_settings})
        or current_selection.get("execution_config") != public_settings("bridge", current_settings)
        or current_selection.get("routing_revision") != current_routing.get("revision")
        or current_selection.get("routing_digest") != current_routing.get("digest")
        or current_routing.get("digest") != digest(routing_cut)
        or not all(current_settings.get(k) for k in CONNECTION)
        or current_settings.get("FORGE_AGENTIC_CHECKPOINTS") != "required"
        or current_settings.get("FORGE_BRIDGE_COMPLETION_RECOVERY") != "required"
        or {k: v for k, v in current_settings.items() if k not in CONNECTION}
        != {k: v for k, v in ineffective_settings.items() if k not in CONNECTION}
    ):
        raise ValueError("The original named selection/profile/routing or nonconnection settings changed")
    result = {
        "version": 1,
        "run_id": run["id"],
        "task_key": key,
        "attempt": task["attempt"],
        "run_digest": digest(run),
        "task_digest": digest(task),
        "journal_digest": entries[-1]["digest"],
        "handoff_digest": handoff["digest"],
        "mode_receipt_digest": record["digest"],
        "mode_claim_digest": tail[2]["digest"],
        "startup_recovery_digest": recovery["digest"] if recovery else None,
        "selection_digest": digest(current_selection),
        "profile_digest": digest(current_profile),
        "routing_digest": current_routing["digest"],
        "original_settings_digest": digest(original_settings),
        "ineffective_settings_digest": digest(ineffective_settings),
        "current_settings_digest": digest(current_settings),
        "frozen_budget_digest": proof.frozen_budget_digest,
    }
    result["digest"] = digest(result)
    return result
