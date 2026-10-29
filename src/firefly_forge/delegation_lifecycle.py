# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Reduce verified native family events into single-claim child observations."""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from firefly_forge.delegation_models import DelegationCancelInput, DelegationStartInput, Identity, StrictRecord

Data = dict[str, Any]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
TERMINAL = frozenset({"completed", "failed", "cancelled", "needs_reconciliation"})


class ChildChange(StrictRecord):
    path: str
    base_sha256: Digest | None
    sha256: Digest | None
    base_executable: bool | None
    executable: bool

    @field_validator("path")
    @classmethod
    def public_path(cls, value: str) -> str:
        DelegationStartInput.public_source_paths([value])
        return value

    @model_validator(mode="after")
    def actual_change(self) -> ChildChange:
        if self.base_sha256 == self.sha256 and self.base_executable == self.executable:
            raise ValueError("Child proposals retain an actual added, changed or deleted file")
        return self


class ChildResult(StrictRecord):
    summary: str = Field(min_length=1)
    changes: list[ChildChange]
    delivery_verified: Literal[False] = False

    @field_validator("delivery_verified", mode="before")
    @classmethod
    def proposal_only(cls, value: object) -> object:
        if value is not False:
            raise ValueError("A child result cannot attest parent integration or delivery")
        return value


class ChildClaim(StrictRecord):
    child_run_id: Identity
    admission_digest: Digest
    generation: Identity
    owner_pid: int = Field(gt=0)
    source_digest: Digest


class ChildTerminal(StrictRecord):
    child_run_id: Identity
    claim_digest: Digest | None
    result: ChildResult | None


class ChildCancelRequested(StrictRecord):
    parent_task_key: Identity
    request: DelegationCancelInput
    claim_digest: Digest | None


def child_lifecycle(events: list[Data]) -> dict[str, Data]:
    """FamilyLedger verifies the enclosing native scalar/hash chain before this reduction."""
    children: dict[str, Data] = {}
    try:
        for event in events:
            kind, payload = event["kind"], event["payload"]
            if kind == "child_admitted":
                request = DelegationStartInput.model_validate(payload["request"])
                identity = payload["child_run_id"]
                if identity in children:
                    raise ValueError("A child has one original admission")
                children[identity] = {
                    "version": 1,
                    "delegation_id": identity,
                    "parent_run_id": payload["parent_run_id"],
                    "parent_task_key": payload["parent_task_key"],
                    "task_id": request.task_id,
                    "request": request.model_dump(mode="json"),
                    "admission_digest": event["digest"],
                    "source_digest": payload["source_digest"],
                    "state": "queued",
                    "claim_digest": None,
                    "receipt_digest": None,
                    "summary": "",
                    "changes": [],
                    "delivery_verified": False,
                }
            elif kind == "child_claimed":
                claim = ChildClaim.model_validate(payload)
                child = children[claim.child_run_id]
                if (
                    child["state"] != "queued"
                    or claim.admission_digest != child["admission_digest"]
                    or claim.source_digest != child["source_digest"]
                ):
                    raise ValueError("A child claim selects its original unclaimed source admission")
                child.update(state="running", claim_digest=event["digest"], claim=claim.model_dump(mode="json"))
            elif kind == "child_cancel_requested":
                cancellation = ChildCancelRequested.model_validate(payload)
                child = children[cancellation.request.delegation_id]
                if (
                    child["state"] not in {"queued", "running"}
                    or cancellation.parent_task_key != child["parent_task_key"]
                    or cancellation.claim_digest != child["claim_digest"]
                ):
                    raise ValueError("Cancellation retains its original parent task and exact child claim")
            elif kind.startswith("child_") and kind.removeprefix("child_") in TERMINAL:
                state = kind.removeprefix("child_")
                terminal = ChildTerminal.model_validate(payload)
                child = children[terminal.child_run_id]
                if child["state"] == "queued":
                    if state != "cancelled" or terminal.claim_digest is not None or terminal.result is not None:
                        raise ValueError("An unclaimed child can only be cancelled without an outcome")
                elif child["state"] != "running" or terminal.claim_digest != child["claim_digest"]:
                    raise ValueError("A terminal child retains its unique original dispatch claim")
                elif (terminal.result is not None) != (state == "completed"):
                    raise ValueError("Only a completed child returns a source analysis or proposal")
                if terminal.result is not None:
                    changes = terminal.result.model_dump(mode="json")["changes"]
                    paths = [change["path"] for change in changes]
                    if len(set(paths)) != len(paths) or not set(paths) <= set(child["request"]["write_paths"]):
                        raise ValueError("A child proposal cannot expand its frozen source ownership")
                    child.update(terminal.result.model_dump(mode="json"))
                child.update(state=state, receipt_digest=event["digest"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Native child lifecycle does not verify") from None
    return copy.deepcopy(children)
