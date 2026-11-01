# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""One finite fresh-verification operation, independent of implementation allowances."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from firefly_forge.execution_requests import ExecutionRequests, digest
from firefly_forge.journal import digest as native_digest

Data = dict[str, Any]
KIND = "linked_verify"


class LinkedVerificationSuccessorV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    predecessor_request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    predecessor_event_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    parent_attempt: int = Field(ge=1)
    output_policy: Literal["vitest_no_cache_v1"]


class LinkedVerificationCommandPlanV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    ordinal: int = Field(ge=1)
    logical_command_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    physical_argv: list[str] = Field(min_length=1)
    output_policy: Literal["unchanged", "vitest_no_cache_v1"]

    @model_validator(mode="after")
    def exact_cache_policy(self) -> LinkedVerificationCommandPlanV1:
        if self.output_policy == "vitest_no_cache_v1" and self.physical_argv[-2:] != ["--", "--no-cache"]:
            raise ValueError("The reviewed Vitest physical plan must explicitly disable its cache")
        return self


class LinkedVerificationInputV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    candidate_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    acknowledge_all_fresh_checks: Literal[True]
    acknowledge_no_coding_or_repair: Literal[True]
    acknowledge_original_history_and_budget_preserved: Literal[True]
    successor: LinkedVerificationSuccessorV1 | None = Field(default=None, exclude_if=lambda value: value is None)
    acknowledge_old_results_not_reused: Literal[True] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("Use the exact linked verification version")
        return value

    @field_validator(
        "acknowledge_all_fresh_checks",
        "acknowledge_no_coding_or_repair",
        "acknowledge_original_history_and_budget_preserved",
        mode="before",
    )
    @classmethod
    def explicit_consent(cls, value: Any) -> Literal[True]:
        if value is not True:
            raise ValueError("Explicit fresh verification consent is required")
        return True

    @field_validator("acknowledge_old_results_not_reused", mode="before")
    @classmethod
    def exact_no_reuse(cls, value: Any) -> Literal[True] | None:
        return None if value is None else cls.explicit_consent(value)

    @model_validator(mode="after")
    def successor_consent(self) -> LinkedVerificationInputV1:
        if (self.successor is None) != (self.acknowledge_old_results_not_reused is None):
            raise ValueError("A successor requires explicit consent that its predecessor results are not reused")
        return self


class LinkedVerificationCandidateV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1] = 1
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    actor_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_contract: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    runner_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_scope_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_tree: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    implementation_receipt_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    terminal_receipt_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    commands_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    effect_budget: int = Field(ge=1)
    original_effect_budget: int = Field(ge=1)
    original_effect_count: int = Field(ge=0)
    owner_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    run_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    journal_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    successor: LinkedVerificationSuccessorV1 | None = Field(default=None, exclude_if=lambda value: value is None)
    execution_plan: list[LinkedVerificationCommandPlanV1] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> int:
        return LinkedVerificationInputV1.exact_version(value)

    @model_validator(mode="after")
    def sealed_candidate(self) -> LinkedVerificationCandidateV1:
        if (self.successor is None) != (self.execution_plan is None):
            raise ValueError("A successor requires its complete visible physical execution plan")
        if self.execution_plan is not None and (
            [item.ordinal for item in self.execution_plan] != list(range(1, self.effect_budget + 1))
            or not any(item.output_policy == "vitest_no_cache_v1" for item in self.execution_plan)
        ):
            raise ValueError("Successor execution plan differs from its full independent effect budget")
        if native_digest(self.model_dump(exclude={"digest"})) != self.digest:
            raise ValueError("Linked verification candidate digest differs")
        return self


class VerificationProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    started: int = Field(ge=0)
    completed: int = Field(ge=0)
    next_ordinal: int | None = Field(ge=1)
    unknown: bool
    failed: bool
    ready_to_settle: bool


class LinkedVerificationSettlementV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    parent_verify_receipt_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    operation_receipt_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_tree: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    commands_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    checks_count: int = Field(ge=1)


class LinkedVerificationFailureV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    ordinal: int = Field(ge=1)
    command_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    exit_code: int
    output_preview: str
    output_truncated: bool
    event_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def actual_failure(self) -> LinkedVerificationFailureV1:
        if self.exit_code == 0 or len(self.output_preview.encode()) > 2000:
            raise ValueError("Failure evidence requires a nonzero result and bounded retained-output preview")
        return self


VerificationHoldPhase = Literal[
    "prepare",
    "admission",
    "command_inputs",
    "dispatch",
    "result_retention",
    "post_result_inputs",
    "post_result_source",
    "post_result_recapture",
    "post_result_authority",
    "settlement",
]


class LinkedVerificationHoldDiagnosticV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    phase: VerificationHoldPhase
    cause_code: Literal[
        "preparation_refused",
        "admission_refused",
        "input_scope_changed",
        "transport_error",
        "result_retention_refused",
        "selected_source_changed",
        "source_capture_refused",
        "authority_refused",
        "settlement_refused",
    ]
    exception_type: Literal[
        "ValueError",
        "RuntimeError",
        "TimeoutError",
        "OSError",
        "PermissionError",
        "FileNotFoundError",
        "TypeError",
        "AssertionError",
        "AccessDenied",
        "Exception",
    ]


class LinkedVerificationReceiptV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1] = 1
    kind: Literal["linked_verify"] = "linked_verify"
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate: LinkedVerificationCandidateV1
    request: LinkedVerificationInputV1
    status: Literal["queued", "executing", "failed", "needs_reconciliation", "completed"]
    progress: VerificationProgress
    event_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    no_coding_grant: Literal[True] = True
    original_budget_preserved: Literal[True] = True
    settlement: LinkedVerificationSettlementV1 | None = Field(default=None, exclude_if=lambda value: value is None)
    last_failure: LinkedVerificationFailureV1 | None = Field(default=None, exclude_if=lambda value: value is None)
    hold_diagnostic: LinkedVerificationHoldDiagnosticV1 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    detail: str = Field(default="", max_length=4000)

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> int:
        return LinkedVerificationInputV1.exact_version(value)

    @field_validator("no_coding_grant", "original_budget_preserved", mode="before")
    @classmethod
    def exact_declarations(cls, value: Any) -> Literal[True]:
        return LinkedVerificationInputV1.explicit_consent(value)

    @model_validator(mode="after")
    def exact_receipt(self) -> LinkedVerificationReceiptV1:
        if (
            self.run_id != self.candidate.run_id
            or self.request_id != self.request.request_id
            or self.request.candidate_digest != self.candidate.digest
            or self.request.successor != self.candidate.successor
            or self.request_digest != digest(self.request.model_dump(mode="json"))
            or not 0 <= self.progress.completed <= self.progress.started <= self.candidate.effect_budget
            or (self.settlement is not None) != (self.status == "completed")
            or self.progress.started - self.progress.completed > 1
            or self.progress.unknown is not (self.progress.started > self.progress.completed)
            or self.progress.failed
            and (self.progress.unknown or self.progress.completed == 0)
        ):
            raise ValueError("Linked verification receipt differs from its exact admission")
        if self.last_failure is not None and (
            not self.progress.failed or self.last_failure.ordinal != self.progress.completed
        ):
            raise ValueError("Failure preview must describe the last known failed result")
        if self.hold_diagnostic is not None and self.status not in {"failed", "needs_reconciliation"}:
            raise ValueError("Hold diagnostics require their actual closed operation")
        if self.status == "queued" and self.progress != VerificationProgress(
            started=0, completed=0, next_ordinal=None, unknown=False, failed=False, ready_to_settle=False
        ):
            raise ValueError("Queued verification cannot claim execution progress")
        if self.status != "executing" and (self.progress.next_ordinal is not None or self.progress.ready_to_settle):
            raise ValueError("Closed or unclaimed verification cannot offer another dispatch")
        if self.status == "failed" and not self.progress.failed:
            raise ValueError("Failed verification requires its actual failed check")
        if self.status == "executing":
            eligible = not self.progress.unknown and not self.progress.failed
            remaining = self.progress.completed < self.candidate.effect_budget
            if self.progress.next_ordinal != (
                self.progress.completed + 1 if eligible and remaining else None
            ) or self.progress.ready_to_settle is not (eligible and not remaining):
                raise ValueError("Executing verification progress differs from its finite next effect")
        if self.settlement is not None and (
            self.settlement.source_digest != self.candidate.source_digest
            or self.settlement.source_tree != self.candidate.source_tree
            or self.settlement.commands_digest != self.candidate.commands_digest
            or self.settlement.checks_count != self.candidate.effect_budget
            or self.progress.completed != self.candidate.effect_budget
            or self.progress.unknown
            or self.progress.failed
        ):
            raise ValueError("Linked verification cannot settle incomplete or foreign checks")
        return self


def linked_verification_frontier(
    receipts: Sequence[LinkedVerificationReceiptV1],
    *,
    run_id: str,
    implementation_receipt_digest: str,
    terminal_receipt_digest: str,
    parent_attempt: int | None = None,
) -> LinkedVerificationReceiptV1 | None:
    """Resolve one validated native successor chain; callers retain all authenticated history."""
    if not receipts:
        return None
    values = {value.request_id: value for value in receipts}
    if len(values) != len(receipts):
        raise ValueError("Linked verification history repeats a native request")
    children: dict[str, str] = {}
    roots = []
    for value in receipts:
        if (
            value.run_id != run_id
            or value.candidate.implementation_receipt_digest != implementation_receipt_digest
            or value.candidate.terminal_receipt_digest != terminal_receipt_digest
        ):
            raise ValueError("Linked verification history belongs to another implementation")
        edge = value.candidate.successor
        if edge is None:
            roots.append(value.request_id)
            continue
        predecessor = values.get(edge.predecessor_request_id)
        if (
            predecessor is None
            or predecessor.request_id == value.request_id
            or predecessor.request_id in children
            or predecessor.event_digest != edge.predecessor_event_digest
            or predecessor.status != "needs_reconciliation"
            or predecessor.progress.unknown
            or predecessor.progress.failed
            or predecessor.progress.started != predecessor.progress.completed
            or predecessor.candidate.source_tree != value.candidate.source_tree
            or predecessor.candidate.commands_digest != value.candidate.commands_digest
            or predecessor.candidate.effect_budget != value.candidate.effect_budget
            or predecessor.candidate.original_effect_budget != value.candidate.original_effect_budget
            or predecessor.candidate.original_effect_count != value.candidate.original_effect_count
            or (parent_attempt is not None and edge.parent_attempt != parent_attempt)
        ):
            raise ValueError("A successor requires its unique known stopped predecessor and exact parent scope")
        children[predecessor.request_id] = value.request_id
    if len(roots) != 1:
        raise ValueError("Linked verification history requires one unambiguous native root")
    current, visited = roots[0], set()
    while current not in visited:
        visited.add(current)
        if current not in children:
            if len(visited) != len(values):
                raise ValueError("Linked verification history contains a disconnected successor")
            return values[current]
        current = children[current]
    raise ValueError("Linked verification successors cannot cycle")


def verification_progress(operation: Data, events: list[Data]) -> VerificationProgress:
    commands, budget = operation["commands"], operation["effect_budget"]
    if (
        operation["kind"] != KIND
        or type(budget) is not int
        or budget < 1
        or budget != len(commands)
        or operation["commands_digest"] != native_digest({"commands": commands})
        or not events
        or len(events) > 2 * budget + 5
        or not ExecutionRequests.verify_chain(events)
        or any(event["request_id"] != operation["id"] for event in events)
    ):
        raise ValueError("Linked verification lost its exact independent command ledger")
    started = completed = 0
    pending: str | None = None
    failed = claimed = finished = False
    for index, event in enumerate(events):
        if finished:
            raise ValueError("A closed verification ledger cannot admit another event")
        kind, evidence = event["kind"], event["evidence"]
        if index == 0:
            if kind != "linked_verification_admitted" or evidence != {"request_digest": operation["request_digest"]}:
                raise ValueError("Linked verification requires its exact native admission")
        elif kind == "linked_verification_claimed":
            if claimed or index != 1 or evidence != {"request_digest": operation["request_digest"]}:
                raise ValueError("A linked verification operation cannot be claimed again")
            claimed = True
        elif kind == "linked_verification_command_intent":
            if not claimed or pending is not None or failed or finished or started == budget:
                raise ValueError("Unknown or closed verification effects cannot execute again")
            ordinal = evidence.get("ordinal")
            if type(ordinal) is not int or evidence != {
                "ordinal": started + 1,
                "command_digest": native_digest(commands[started]),
                "inputs_digest": operation["inputs_digest"],
            }:
                raise ValueError("Verification effect differs from its exact declared command or inputs")
            started += 1
            pending = event["digest"]
        elif kind == "linked_verification_command_result":
            if (
                pending is None
                or set(evidence) != {"ordinal", "intent_digest", "exit_code", "output"}
                or type(evidence["ordinal"]) is not int
                or evidence["ordinal"] != started
                or evidence["intent_digest"] != pending
                or type(evidence["exit_code"]) is not int
                or type(evidence["output"]) is not str
                or len(evidence["output"].encode()) > 32000
            ):
                raise ValueError("Verification result lost its unique actual effect intent")
            completed += 1
            failed = evidence["exit_code"] != 0
            pending = None
        elif kind == "linked_verification_completed":
            if finished or pending is not None or failed or completed != budget:
                raise ValueError("Parent verification requires all independent fresh checks to succeed")
            if evidence != {"inputs_digest": operation["inputs_digest"], "completed": budget}:
                raise ValueError("Linked verification completion changed its input or effect count")
            finished = True
        elif kind == "linked_verification_held":
            if (
                finished
                or set(evidence) not in ({"reason"}, {"reason", "diagnostic"})
                or evidence["reason"]
                not in {
                    "check_failed",
                    "inputs_changed",
                    "unknown",
                    "shutdown",
                }
            ):
                raise ValueError("Verification hold cannot replace a closed native outcome")
            if "diagnostic" in evidence:
                LinkedVerificationHoldDiagnosticV1.model_validate(evidence["diagnostic"])
            finished = True
        else:
            raise ValueError("Unrecognized activity cannot extend a verification-only operation")
    return VerificationProgress(
        started=started,
        completed=completed,
        next_ordinal=started + 1
        if claimed and not pending and not failed and not finished and started < budget
        else None,
        unknown=pending is not None,
        failed=failed,
        ready_to_settle=claimed and not finished and not failed and completed == budget,
    )
