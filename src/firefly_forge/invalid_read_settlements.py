# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Validate a native negative-read settlement without relabeling a worker report."""

from typing import Any

from firefly_forge.agent_checkpoint_models import InvocationBinding
from firefly_forge.invalid_read_continuity_models import (
    InvalidReadAdmissionInputV1,
    InvalidReadClaimV1,
    InvalidReadProofV1,
    InvalidReadSettlementV1,
)
from firefly_forge.invalid_read_lease import ContinuityLeasePolicyV1, lease_window
from firefly_forge.journal import digest

SETTLEMENT = "invalid_read_settlement"
CLAIM = "invalid_read_claim"
KIND = "invalid_read"


def settled_requests(entry: dict[str, Any], entries: list[dict[str, Any]]) -> list[str]:
    value = InvalidReadSettlementV1.model_validate(entry["payload"]).model_dump()
    proof = InvalidReadProofV1.model_validate(value["proof"])
    request = InvalidReadAdmissionInputV1.model_validate(value["request"])
    if (
        entry["kind"] != SETTLEMENT
        or proof.run_id != entry["run_id"]
        or entry["task_id"] != "implement"
        or digest({k: v for k, v in value.items() if k != "digest"}) != value.get("digest")
        or digest({k: v for k, v in proof.model_dump().items() if k != "digest"}) != proof.digest
        or request.expected_policy_digest != value["lease_policy"]["digest"]
        or request.expected_run_digest != proof.run_digest
        or request.expected_journal_digest != proof.journal_digest
        or request.candidate_digest != value["candidate_digest"]
        or entry["idempotency_key"] != "invalid-read:" + value["request_id"]
    ):
        raise ValueError("Invalid-read settlement lost its exact native scope")
    policy = value["lease_policy"]
    epoch = value["policy_epoch"]
    if epoch is not None:
        from firefly_forge.invalid_read_epochs import LEGACY_EPOCH

        if (
            digest({k: v for k, v in epoch.items() if k != "digest"}) != epoch.get("digest")
            or epoch.get("legacy") != LEGACY_EPOCH
            or epoch.get("run_id") != proof.run_id
            or epoch.get("legacy_runtime_contract") != proof.runtime_contract
            or epoch.get("current_profile") != value["current_profile"]
            or epoch.get("lease_policy_digest") != request.expected_policy_digest
        ):
            raise ValueError("Negative-read settlement has an incompatible frozen policy epoch")
    if digest({k: v for k, v in policy.items() if k != "digest"}) != policy["digest"] or not policy["revision"]:
        raise ValueError("Invalid-read settlement policy revision is unavailable")
    lease = value["lease"]
    expected = lease_window(
        ContinuityLeasePolicyV1.model_validate(policy["settings"]),
        frozen_timeout_seconds=value["frozen_timeout_seconds"],
        valid_for_seconds=request.valid_for_seconds,
        now_ms=lease["issued_at_ms"],
        original_deadline_ms=proof.deadline_ms,
        active_deadline_ms=lease["active_deadline_ms"],
    )
    if lease != expected:
        raise ValueError("Invalid-read ownership lease does not verify")
    original = InvocationBinding.model_validate(value["original_binding"])
    binding = InvocationBinding.model_validate(value["binding"])
    if (
        original.contract != proof.runtime_contract
        or original.round != proof.attempt
        or original.deadline_ms != proof.deadline_ms
        or binding.model_dump()
        != {
            **original.model_dump(),
            "invocation_id": binding.invocation_id,
            "ordinal": 1,
            "predecessor": None,
            "deadline_ms": lease["expires_at_ms"],
            "contract": binding.contract,
        }
        or binding.invocation_id == original.invocation_id
    ):
        raise ValueError("Negative-read migration changed the original task scope")
    reconstruction = value["reconstruction"]
    if (
        digest({k: v for k, v in reconstruction.items() if k != "digest"}) != reconstruction["digest"]
        or reconstruction["proof_digest"] != proof.digest
        or reconstruction["original_checkpoint"] != proof.checkpoint_digest
        or reconstruction["migrated_checkpoint"] != value["checkpoint_digest"]
        or reconstruction["known_consumption_reused"] != proof.consume_record_digest
        or reconstruction["external_requests"] != 0
        or reconstruction["native_tool_callbacks"] != 0
    ):
        raise ValueError("Negative-read reconstruction has incompatible custody")
    closed = []
    for reference in proof.completions:
        item = reference.model_dump()
        for kind, field in zip(
            ("bridge_request_intent", "agent_checkpoint", "bridge_request_response", "agent_consume"),
            ("intent_digest", "checkpoint_record_digest", "response_record_digest", "consume_record_digest"),
            strict=True,
        ):
            matches = [e for e in entries if e["kind"] == kind and e["idempotency_key"] == item["request_id"]]
            if (
                len(matches) != 1
                or matches[0]["run_id"] != entry["run_id"]
                or matches[0]["digest"] != item[field]
                or matches[0]["sequence"] >= entry["sequence"]
            ):
                raise ValueError("Negative-read settlement lost its original consumed closure")
        if item["request_id"] in closed:
            raise ValueError("Negative-read settlement duplicated an original completion")
        closed.append(item["request_id"])
    original_checkpoints = [e for e in entries if e["digest"] == proof.checkpoint_record_digest]
    if len(original_checkpoints) != 1 or original_checkpoints[0]["payload"]["invocation"] != original.model_dump():
        raise ValueError("Negative-read migration lost its original invocation binding")
    claims = [e for e in entries if e["kind"] == CLAIM and e["payload"].get("settlement_digest") == entry["digest"]]
    if len(claims) != 1 or claims[0]["sequence"] != entry["sequence"] + 1 or claims[0]["run_id"] != entry["run_id"]:
        raise ValueError("Negative-read settlement lacks its one native ownership claim")
    claim = InvalidReadClaimV1.model_validate(claims[0]["payload"]).model_dump()
    if (
        claims[0]["task_id"] != "implement"
        or claims[0]["idempotency_key"] != "invalid-claim:" + value["request_id"]
        or claim
        != {
            "version": 1,
            "request_id": value["request_id"],
            "task_key": proof.task_key,
            "attempt": proof.attempt,
            "settlement_digest": entry["digest"],
            "lease_generation": binding.invocation_id,
            "expires_at_ms": lease["expires_at_ms"],
        }
    ):
        raise ValueError("Negative-read ownership claim differs from its exact same-attempt lease")
    return closed
