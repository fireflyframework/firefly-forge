# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit environment-only authority for an already queued native continuation."""

from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator

Data = dict[str, Any]
SOURCE_REFUSAL = "Generated changes contain private credentials or worker metadata; inspect the workspace"


class WorkspaceReconciliationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    candidate_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    acknowledge_environment_only: Literal[True]
    acknowledge_resume_existing_task: Literal[True]

    @field_validator("acknowledge_environment_only", "acknowledge_resume_existing_task", mode="before")
    @classmethod
    def explicit_acknowledgement(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("Explicitly acknowledge the environment-only existing-task transition")
        return value


def queued_environment_scope(
    run: Data,
    task: Data,
    entries: list[Data],
    *,
    verification_allowance: int,
    human_allowance: int,
    remaining: int,
    final_checks: int,
) -> Data:
    """Admit no task, provider dispatch or runner budget beyond the existing native plan."""
    attempt, rounds, repairs = task.get("attempt"), run.get("implementation_rounds"), run.get("repair_budget")
    if type(attempt) is not int or type(rounds) is not int or type(repairs) is not int:
        raise ValueError("The original queued-task counters must be exact native integers")
    if (
        any(type(value) is not int for value in (verification_allowance, human_allowance))
        or run.get("status") != "failed"
        or run.get("active_task") != "implement"
        or run.get("error") != SOURCE_REFUSAL
        or task.get("id") != "implement"
        or task.get("status") != "queued"
        or not 2 <= attempt <= 4
        or rounds != attempt - 1
        or not 0 <= repairs <= 3
        or not 0 <= verification_allowance <= 3
        or not 0 <= human_allowance <= 3
        or run.get("provider_recovery")
        or run.get("bridge_completion_hold")
        or run.get("cancel_request", {}).get("status") == "requested"
        or run.get("worker_request", {}).get("status") == "pending"
    ):
        raise ValueError("Only a known pre-dispatch environment refusal of an existing queued task can be reconciled")
    limit = min(4, repairs + 1 + verification_allowance + human_allowance)
    if attempt > limit or not 1 <= final_checks <= 20 or remaining < final_checks:
        raise ValueError("Reconciliation cannot expand implementation or final-verification budgets")
    state = run.get("verification_handoff", {})
    handoff = state.get("id")
    next_key = uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:{attempt}").hex
    if (
        not handoff
        or state.get("status") != "completed"
        or run.get("worker_response", {}).get("verification_handoff_id") != handoff
        or not any(
            entry["kind"] == "verification_handoff_outcome"
            and entry["payload"].get("id") == handoff
            and entry["payload"].get("status") == "completed"
            for entry in entries
        )
        or not any(
            entry["kind"] == "resume"
            and entry["payload"].get("automatic") is True
            and entry["payload"].get("verification_handoff_id") == handoff
            for entry in entries
        )
        or any(
            entry["kind"] in {"provider_dispatch_intent", "intent"}
            and (entry.get("task_id") == "implement" or entry["kind"] == "provider_dispatch_intent")
            and (entry.get("idempotency_key") == next_key or entry["payload"].get("attempt", 0) >= attempt)
            for entry in entries
        )
    ):
        raise ValueError("The queued continuation must retain a completed native handoff and no next dispatch intent")
    return {
        "attempt": attempt,
        "implementation_limit": limit,
        "remaining_commands": remaining,
        "final_checks": final_checks,
    }
