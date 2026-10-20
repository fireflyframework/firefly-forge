# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit failed-process recovery, distinct from verification and repair grants."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Data = dict[str, Any]


class ProviderAttemptClosed(ValueError):
    """A late old invocation must not mutate its successor run state."""


class ProviderProcessOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    exit_code: int = Field(ge=-2147483648, le=2147483647)
    leader_waited: bool
    streams_drained: bool
    group_absent: bool

    @property
    def known_failure(self) -> bool:
        return self.exit_code != 0 and self.leader_waited and self.streams_drained and self.group_absent


class LegacyProcessAttestation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provenance: Literal["legacy_operator_attestation"]
    evidence_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    trusted_operator_observation: bool
    process_terminated: bool
    process_group_drained: bool
    no_accepted_final_result: bool
    exit_code: int = Field(ge=-2147483648, le=2147483647)

    @field_validator(
        "trusted_operator_observation", "process_terminated", "process_group_drained", "no_accepted_final_result"
    )
    @classmethod
    def acknowledged(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Explicit trusted operator observation is required")
        return value

    @field_validator("exit_code")
    @classmethod
    def failed(cls, value: int) -> int:
        if value == 0:
            raise ValueError("A successful process cannot be adopted as a failure")
        return value


class ProviderRecoveryAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    candidate_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    agent: Literal["codex", "claude"]
    native_process_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    legacy_attestation: LegacyProcessAttestation | None = None

    @model_validator(mode="after")
    def one_proof(self) -> ProviderRecoveryAdmission:
        if (self.native_process_digest is None) == (self.legacy_attestation is None):
            raise ValueError("Choose one native receipt or explicit legacy operator attestation")
        return self


def dispatch_keys(entries: list[Data]) -> set[str]:
    keys = set()
    for entry in entries:
        if entry.get("task_id") != "implement":
            continue
        if entry.get("kind") == "intent":
            keys.add(str(entry["idempotency_key"]))
        elif entry.get("kind") == "provider_dispatch_intent":
            keys.add(str(entry["payload"]["task_key"]))
    return keys


def failed_implementation(run: Data, task: Data, entries: list[Data]) -> Data:
    if (
        run.get("status") != "failed"
        or run.get("agent") not in {"codex", "claude"}
        or run.get("active_task") != "implement"
        or task.get("status") not in {"running", "failed", "needs_reconciliation"}
        or task.get("id") != "implement"
        or (run.get("cancel_request") or {}).get("status") == "requested"
        or run.get("bridge_completion_hold")
    ):
        raise ValueError("Recovery requires the current failed official-provider implementation")
    key = task.get("idempotency_key")
    intent = next(
        (e for e in entries if e["kind"] == "intent" and e["task_id"] == "implement" and e["idempotency_key"] == key),
        None,
    )
    if intent is None or any(e["kind"] == "receipt" and e["idempotency_key"] == key for e in entries):
        raise ValueError("The failed implementation intent is unavailable or already completed")
    return intent
