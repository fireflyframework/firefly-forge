# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Finite execution windows; project cardinality never grants execution capacity."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CampaignExecutionPolicyV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = Field(default=True, strict=True)
    dispatch_window: int = Field(default=50, gt=0, strict=True)
    max_parallel_runs: int = Field(default=4, gt=0, strict=True)


class CampaignExecutionPolicyInputV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_previous_digest: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    policy: CampaignExecutionPolicyV1


class CampaignBudgetRenewalV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_journal_head: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    additional_dispatches: int = Field(gt=0, strict=True)
    max_parallel_runs: int = Field(gt=0, strict=True)


class CampaignBudgetSnapshotV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    policy: CampaignExecutionPolicyV1
    policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class CampaignBudgetGrantV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    additional_dispatches: int = Field(gt=0, strict=True)
    previous_granted_dispatches: int = Field(gt=0, strict=True)
    granted_dispatches: int = Field(gt=0, strict=True)
    max_parallel_runs: int = Field(gt=0, strict=True)
    policy: CampaignExecutionPolicyV1
    policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: dict[str, Any]


def freeze_budget(policy: dict[str, Any], policy_digest: str) -> dict[str, Any]:
    frozen = CampaignBudgetSnapshotV1(
        policy=CampaignExecutionPolicyV1.model_validate(policy), policy_digest=policy_digest
    )
    if not frozen.policy.enabled:
        raise ValueError("Campaign execution budget policy is disabled")
    return frozen.model_dump(mode="json")


def derive_budget(frozen: Any, entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Project a previously verified campaign chain without resetting old reservations."""
    if frozen is None:
        return None
    try:
        initial = CampaignBudgetSnapshotV1.model_validate(frozen)
        if not initial.policy.enabled:
            raise ValueError("Disabled initial grant")
        granted = initial.policy.dispatch_window
        parallel = initial.policy.max_parallel_runs
        dispatched: set[str] = set()
        for entry in entries:
            if entry["kind"] == "released":
                run_id = entry["payload"]["run_id"]
                if not isinstance(run_id, str) or len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
                    raise ValueError("Invalid dispatch identity")
                if run_id in dispatched or len(dispatched) >= granted:
                    raise ValueError("Duplicate or unreserved dispatch")
                dispatched.add(run_id)
            elif entry["kind"] == "budget_renewal":
                renewal = CampaignBudgetGrantV1.model_validate(entry["payload"])
                if (
                    not renewal.policy.enabled
                    or renewal.previous_granted_dispatches != granted
                    or renewal.granted_dispatches != granted + renewal.additional_dispatches
                    or renewal.additional_dispatches > renewal.policy.dispatch_window
                    or renewal.max_parallel_runs > renewal.policy.max_parallel_runs
                ):
                    raise ValueError("Invalid additional reservation")
                granted = renewal.granted_dispatches
                parallel = renewal.max_parallel_runs
    except (KeyError, TypeError, ValueError):
        raise ValueError("Campaign execution budget history does not verify") from None
    return {
        "version": 1,
        "granted_dispatches": granted,
        "consumed_dispatches": len(dispatched),
        "remaining_dispatches": granted - len(dispatched),
        "max_parallel_runs": parallel,
    }
