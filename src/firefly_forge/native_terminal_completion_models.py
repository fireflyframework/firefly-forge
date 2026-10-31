# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native acceptance of known terminal output; no operator or execution grant."""

from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from firefly_forge.final_outcome_models import RetainedVerifiedSourceV1
from firefly_forge.journal import digest
from firefly_forge.known_worker_completion_models import CompletionCustodyV1, CompletionHoldV1
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract


class RestoredCompletionCustodyV2(CompletionCustodyV1):
    consume_version: Literal[2]
    source_kind: Literal["http_response", "bridge_observation"]
    restore_request_id: NativeID
    generation: NativeID
    restore_ticket_digest: Digest
    restore_events_digest: Digest


class FinalOutcomeCompletionCustodyV1(CompletionCustodyV1):
    consume_version: Literal[2]
    custody: Literal["known_final_outcome_v1"]
    source_kind: Literal["http_response", "bridge_observation"]
    restore_request_id: NativeID
    generation: NativeID
    final_outcome_witness_digest: Digest
    original_ticket_digest: Digest
    original_events_digest: Digest


class ConsumedTerminalCustodyV2(CompletionCustodyV1):
    consume_version: Literal[1]
    custody: Literal["consumed_terminal_v2"]
    recovery_witness_digest: Digest
    reconciliation_record_digest: Digest | None = Field(default=None, exclude_if=lambda value: value is None)


class NativeTerminalProofV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    run_id: NativeID
    task_id: Literal["implement"]
    task_attempt: int = Field(ge=1)
    task_key: str = Field(min_length=1, max_length=64)
    runtime_contract: Digest
    completion_count: int = Field(ge=1)
    completions: list[
        CompletionCustodyV1 | RestoredCompletionCustodyV2 | FinalOutcomeCompletionCustodyV1 | ConsumedTerminalCustodyV2
    ]
    terminal_report_sha256: Digest
    final_response_sha256: Digest
    observed_hold_digest: Digest
    acceptance_kind: Literal["receipt", "worker_feedback"]
    acceptance_record_digest: Digest
    accepted_payload_sha256: Digest
    source_checkpoint: dict[str, Any] | None
    verification: Literal["native_terminal_output_known_only"]
    retained_source: RetainedVerifiedSourceV1 | None = Field(default=None, exclude_if=lambda value: value is None)
    digest: Digest

    @model_validator(mode="after")
    def exact_proof(self) -> "NativeTerminalProofV1":
        if (
            len(self.completions) != self.completion_count
            or len({item.request_id for item in self.completions}) != self.completion_count
            or (self.acceptance_kind == "receipt") != (self.source_checkpoint is not None)
            or digest(self.model_dump(exclude={"digest"})) != self.digest
        ):
            raise ValueError("Native terminal proof differs from accepted immutable custody")
        return self


class NativeTerminalReceiptV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    run_id: NativeID
    producer: Literal["native_implementation_receipt", "native_worker_feedback"]
    actor_digest: Digest
    proof: NativeTerminalProofV1
    observed_hold: CompletionHoldV1

    @model_validator(mode="after")
    def exact_receipt(self) -> "NativeTerminalReceiptV1":
        if (
            self.proof.run_id != self.run_id
            or self.producer
            != (
                "native_implementation_receipt" if self.proof.acceptance_kind == "receipt" else "native_worker_feedback"
            )
            or digest(self.observed_hold.model_dump(mode="json")) != self.proof.observed_hold_digest
        ):
            raise ValueError("Native terminal receipt differs from its actual acceptance")
        return self


def validate_native_receipt(entry: dict[str, Any], entries: list[dict[str, Any]]) -> NativeTerminalReceiptV1:
    value = NativeTerminalReceiptV1.model_validate(entry["payload"])
    for closure in value.proof.completions:
        if isinstance(closure, ConsumedTerminalCustodyV2):
            from firefly_forge.consumed_terminal_completions import validate_witness as validate_consumed_witness

            witnesses = [e for e in entries if e["digest"] == closure.recovery_witness_digest]
            if len(witnesses) != 1 or witnesses[0]["sequence"] >= entry["sequence"]:
                raise ValueError("Consumed terminal receipt lost its native reviewed witness")
            witness = validate_consumed_witness(witnesses[0], entries)
            expected = {
                k: v
                for k, v in closure.model_dump(mode="json").items()
                if k not in {"consume_version", "custody", "recovery_witness_digest", "reconciliation_record_digest"}
            }
            runtime = witness["candidate"]["proof"]["current_runtime_contract"]
            reconciliations = [
                e
                for e in entries
                if e["kind"] == "consumed_terminal_reconciliation"
                and e["payload"].get("candidate", {}).get("recovery_digest") == witnesses[0]["digest"]
            ]
            if reconciliations:
                from firefly_forge.consumed_terminal_reconciliations import validate_record

                if (
                    len(reconciliations) != 1
                    or closure.reconciliation_record_digest != reconciliations[0]["digest"]
                    or not witnesses[0]["sequence"] < reconciliations[0]["sequence"] < entry["sequence"]
                ):
                    raise ValueError("Consumed terminal receipt lost its exact native reconciliation")
                authority = validate_record(reconciliations[0], entries)
                runtime = authority["candidate"]["runtime_contract"]
            elif closure.reconciliation_record_digest is not None:
                raise ValueError("Consumed terminal receipt names a foreign reconciliation")
            if (
                expected not in witness["candidate"]["proof"]["completions"]
                or value.proof.runtime_contract != runtime
                or value.proof.terminal_report_sha256 != witness["candidate"]["proof"]["terminal_report_sha256"]
            ):
                raise ValueError("Consumed terminal receipt changed its exact reviewed response")
        if isinstance(closure, FinalOutcomeCompletionCustodyV1):
            from firefly_forge.final_outcome_continuations import validate_witness

            witnesses = [e for e in entries if e["digest"] == closure.final_outcome_witness_digest]
            consumed = [e for e in entries if e["digest"] == closure.consume_record_digest]
            if len(witnesses) != 1 or len(consumed) != 1:
                raise ValueError("Known final receipt lost its original handoff or consumed response")
            validate_witness(witnesses[0], entries)
            p = witnesses[0]["payload"]
            if (
                witnesses[0]["sequence"] >= entry["sequence"]
                or consumed[0]["sequence"] >= witnesses[0]["sequence"]
                or digest(p["failed_ticket"]) != closure.original_ticket_digest
                or digest({"events": p["events"]}) != closure.original_events_digest
                or p["failed_ticket"]["id"] != closure.restore_request_id
                or p["failed_ticket"]["generation"] != closure.generation
                or consumed[0]["payload"].get("restore_request_id") != closure.restore_request_id
                or consumed[0]["payload"].get("generation") != closure.generation
            ):
                raise ValueError("Known final receipt changed the previously consumed native claim")
    if value.proof.retained_source is not None:
        from firefly_forge.retained_verified_source import validate_retained_source_receipt

        validate_retained_source_receipt(value.proof.retained_source, entry, entries)
    matches = [record for record in entries if record["digest"] == value.proof.acceptance_record_digest]
    if (
        entry["kind"] != "terminal_completion_receipt"
        or entry["run_id"] != value.run_id
        or entry["task_id"] != "implement"
        or entry["idempotency_key"] != "terminal-completion:" + value.proof.acceptance_record_digest[:40]
        or len(matches) != 1
        or matches[0]["run_id"] != value.run_id
        or matches[0]["task_id"] != "implement"
        or matches[0]["kind"] != value.proof.acceptance_kind
        or matches[0]["sequence"] >= entry["sequence"]
        or digest(matches[0]["payload"]) != value.proof.accepted_payload_sha256
        or matches[0]["kind"] == "receipt"
        and (
            matches[0]["idempotency_key"] != value.proof.task_key
            or matches[0]["payload"].get("source_tree") != (value.proof.source_checkpoint or {}).get("source_tree")
        )
    ):
        raise ValueError("Native terminal receipt lost its exact accepted journal row")
    return value
