# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Validated native verification facts confer no execution or SCM proposal authority."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from firefly_forge.journal import digest
from firefly_forge.linked_verification_models import LinkedVerificationReceiptV1, linked_verification_frontier
from firefly_forge.scm_authority_models import Digest, NativeID, StrictContract


class SCMNativeVerificationResultV1(StrictContract):
    ordinal: int = Field(ge=1)
    exit_code: int
    command_digest: Digest
    event_digest: Digest


class SCMNativeVerificationOperationV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    receipt: LinkedVerificationReceiptV1
    results: tuple[SCMNativeVerificationResultV1, ...]
    final_event_kind: Literal[
        "linked_verification_admitted",
        "linked_verification_claimed",
        "linked_verification_command_intent",
        "linked_verification_command_result",
        "linked_verification_held",
        "linked_verification_completed",
    ]
    hold_reason: Literal["check_failed", "inputs_changed", "unknown", "shutdown"] | None = None

    @property
    def known_passed(self) -> int:
        return sum(result.exit_code == 0 for result in self.results)

    @property
    def known_failed(self) -> int:
        return sum(result.exit_code != 0 for result in self.results)

    @model_validator(mode="after")
    def exact_results(self) -> SCMNativeVerificationOperationV1:
        if (
            tuple(result.ordinal for result in self.results) != tuple(range(1, self.receipt.progress.completed + 1))
            or self.receipt.progress.failed is not bool(self.known_failed)
            or any(result.exit_code != 0 for result in self.results[:-1])
            or (self.final_event_kind == "linked_verification_held") != (self.hold_reason is not None)
        ):
            raise ValueError("Native verification facts require every exact known result and its original ordinal")
        return self


def native_verification_facts(view: SCMNativeVerificationStatusV1, run: dict[str, Any]) -> dict[str, Any]:
    """Expose validated counts and custody references, never stored output or execution authority."""
    view = SCMNativeVerificationStatusV1.model_validate_json(view.model_dump_json())
    if (
        any(
            getattr(view, key) != run.get("id" if key == "run_id" else key)
            for key in ("run_id", "project_id", "repository_id", "component_id", "attempt")
        )
        or digest(run) != view.parent_run_digest
    ):
        raise ValueError("Native verification facts require the exact current parent run")

    def summary(operation: SCMNativeVerificationOperationV1) -> dict[str, Any]:
        receipt = operation.receipt
        return dict(
            request_id=receipt.request_id,
            status=receipt.status,
            required=receipt.candidate.effect_budget,
            **receipt.progress.model_dump(),
            known_passed=operation.known_passed,
            known_failed=operation.known_failed,
            event_digest=receipt.event_digest,
            hold_reason=operation.hold_reason,
            hold_diagnostic=receipt.hold_diagnostic.model_dump() if receipt.hold_diagnostic is not None else None,
            results_digest=digest({"results": [result.model_dump(mode="json") for result in operation.results]}),
        )

    current = next((op for op in view.operations if op.receipt.request_id == view.current_request_id), None)
    facts = dict(
        source=view.source,
        projection_digest=view.digest,
        parent_attempt=view.attempt,
        implementation_receipt_digest=view.implementation_receipt_digest,
        terminal_receipt_digest=view.terminal_receipt_digest,
        source_tree=view.source_tree,
        current=summary(current) if current else None,
        historical_operations=[summary(op) for op in view.operations if op is not current],
        other_implementation_operations=view.other_implementation_operations,
        other_implementation_digest=view.other_implementation_digest,
        old_results_reused=False,
        execution_grant=False,
    )
    if view.held_predecessor is not None:
        facts["held_predecessor"] = view.held_predecessor.model_dump(mode="json")
    return facts


class SCMNativeVerificationPredecessorV1(StrictContract):
    request_id: NativeID
    event_digest: Digest
    parent_attempt: int = Field(ge=1)
    review_only: Literal[True] = True

    @field_validator("review_only", mode="before")
    @classmethod
    def only_review(cls, value: object) -> object:
        if value is not True:
            raise ValueError("A held predecessor is a review reference, never an admission")
        return value


class SCMNativeVerificationStatusV1(StrictContract):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1] = 1
    source: Literal["validated_native_request_events"] = "validated_native_request_events"
    run_id: NativeID
    project_id: NativeID
    repository_id: NativeID
    component_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    attempt: int = Field(ge=1)
    implementation_receipt_digest: Digest
    terminal_receipt_digest: Digest
    source_tree: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    read_scope_digest: Digest
    parent_run_digest: Digest
    current_request_id: NativeID | None
    operations: tuple[SCMNativeVerificationOperationV1, ...]
    other_implementation_operations: int = Field(ge=0)
    other_implementation_digest: Digest
    held_predecessor: SCMNativeVerificationPredecessorV1 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    execution_grant: Literal[False] = False
    digest: Digest

    @field_validator("execution_grant", mode="before")
    @classmethod
    def observation_only(cls, value: object) -> object:
        if value is not False:
            raise ValueError("A native verification status cannot confer execution authority")
        return value

    @model_validator(mode="after")
    def exact_parent(self) -> SCMNativeVerificationStatusV1:
        identities = tuple(operation.receipt.request_id for operation in self.operations)
        if (
            len(set(identities)) != len(identities)
            or (self.current_request_id is None) != (not self.operations)
            or self.current_request_id is not None
            and self.current_request_id not in identities
            or any(
                operation.receipt.run_id != self.run_id
                or operation.receipt.candidate.implementation_receipt_digest != self.implementation_receipt_digest
                or operation.receipt.candidate.terminal_receipt_digest != self.terminal_receipt_digest
                or operation.receipt.candidate.source_tree != self.source_tree
                for operation in self.operations
            )
            or digest(self.model_dump(mode="json", exclude={"digest"})) != self.digest
        ):
            raise ValueError("Native verification status differs from its accepted parent or validated frontier")
        frontier = linked_verification_frontier(
            [operation.receipt for operation in self.operations],
            run_id=self.run_id,
            implementation_receipt_digest=self.implementation_receipt_digest,
            terminal_receipt_digest=self.terminal_receipt_digest,
            parent_attempt=self.attempt,
        )
        if (frontier.request_id if frontier else None) != self.current_request_id:
            raise ValueError("Native verification status must retain its validated current successor frontier")
        if predecessor := self.held_predecessor:
            current = next(
                (operation for operation in self.operations if operation.receipt.request_id == self.current_request_id),
                None,
            )
            if (
                current is None
                or predecessor.request_id != self.current_request_id
                or predecessor.event_digest != current.receipt.event_digest
                or predecessor.parent_attempt != self.attempt
                or current.receipt.status != "needs_reconciliation"
                or current.final_event_kind != "linked_verification_held"
                or current.receipt.progress.started != current.receipt.progress.completed
                or current.receipt.progress.unknown
                or current.receipt.progress.failed
            ):
                raise ValueError("Only the exact known closed current frontier can be offered for fresh review")
        return self
