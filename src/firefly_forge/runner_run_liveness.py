# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit per-run ownership; no factory-wide heartbeat grants another work lease."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

RUN_LIVENESS_PROTOCOL = "forge-run-owned-liveness-v1"
RUN_ADMISSION_PROTOCOL = "forge-run-owned-admission-v1"
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Epoch = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class RunLivenessBindingV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    protocol: Literal["forge-run-owned-liveness-v1"] = "forge-run-owned-liveness-v1"
    task_id: Literal["implement", "verify"]
    attempt: int = Field(gt=0)
    snapshot_digest: Digest
    actor_digest: Digest
    run_cut_digest: Digest
    factory_fence_digest: Digest
    worker_epoch: Epoch


class RunLeaseAckV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    protocol: Literal["forge-run-lease-v1"] = "forge-run-lease-v1"
    invocation_digest: Digest
    worker_epoch: Epoch
    controller_epoch: Epoch
    sequence: int = Field(ge=0, le=2**63 - 1)
    challenge: Epoch


class RunRenewalV1(RunLeaseAckV1):
    protocol: Literal["forge-run-renewal-v1"] = "forge-run-renewal-v1"  # type: ignore[assignment]
    sequence: int = Field(gt=0, le=2**63 - 1)


def run_digest(value: Any) -> str:
    def identifier(item: Any) -> str:
        if isinstance(item, UUID):
            return item.hex
        raise TypeError("Run authority must contain only JSON data")

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=identifier).encode()
    ).hexdigest()


def run_task_key(run_id: str, task_id: str, attempt: int) -> str:
    return uuid5(NAMESPACE_URL, f"forge:{UUID(run_id).hex}:{task_id}:{attempt}").hex


def run_stage_active(run: dict[str, Any], task_id: str) -> bool:
    return (
        task_id in {"implement", "verify"}
        and run.get("status") == {"implement": "developing", "verify": "validating"}[task_id]
        and run.get("active_task") == task_id
    )


def run_cut_digest(run: dict[str, Any]) -> str:
    return run_digest(
        {
            key: run.get(key)
            for key in (
                "repository_id",
                "project_id",
                "component_snapshot",
                "execution_actor",
                "sandbox_snapshot",
                "work_duration",
                "agent",
                "publish",
                "operation",
            )
        }
    )


def validate_run_ack(
    value: Any,
    invocation_digest: str,
    binding: RunLivenessBindingV1,
    *,
    sequence: int,
    controller_epoch: str | None = None,
) -> dict[str, Any]:
    result = RunLeaseAckV1.model_validate(value)
    if (
        result.invocation_digest != invocation_digest
        or result.worker_epoch != binding.worker_epoch
        or result.sequence != sequence
        or controller_epoch is not None
        and result.controller_epoch != controller_epoch
    ):
        raise ValueError("Run ownership acknowledgement changed its frozen scope")
    return result.model_dump(mode="json")


class RunLease:
    def __init__(
        self,
        invocation_digest: str,
        binding: RunLivenessBindingV1,
        controller_epoch: str,
        grace_seconds: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            re.fullmatch(r"[a-f0-9]{64}", invocation_digest) is None
            or re.fullmatch(r"[a-f0-9]{32}", controller_epoch) is None
            or type(grace_seconds) is not int
            or not 1 <= grace_seconds <= 120
        ):
            raise ValueError("Invalid run ownership identity or grace")
        self.invocation_digest, self.binding = invocation_digest, binding
        self.controller_epoch, self.grace_seconds, self.clock = controller_epoch, grace_seconds, clock
        self.sequence, self.closed = 0, False
        self.challenge = secrets.token_hex(16)
        self.expires = clock() + grace_seconds

    def ack(self) -> dict[str, Any]:
        return RunLeaseAckV1(
            invocation_digest=self.invocation_digest,
            worker_epoch=self.binding.worker_epoch,
            controller_epoch=self.controller_epoch,
            sequence=self.sequence,
            challenge=self.challenge,
        ).model_dump(mode="json")

    def remaining(self) -> float:
        if self.closed:
            raise ValueError("Run ownership is closed")
        remaining = self.expires - self.clock()
        if remaining <= 0:
            self.closed = True
            raise ValueError("Run ownership expired; reconciliation required")
        return remaining

    def check(self, request: RunRenewalV1) -> None:
        if (
            request.invocation_digest != self.invocation_digest
            or request.worker_epoch != self.binding.worker_epoch
            or request.controller_epoch != self.controller_epoch
            or request.sequence != self.sequence + 1
            or request.challenge != self.challenge
        ):
            raise ValueError("Run ownership renewal is stale or selects another owner")

    def renew(self, request: RunRenewalV1) -> dict[str, Any]:
        self.check(request)
        self.remaining()
        challenge = secrets.token_hex(16)
        now = self.clock()
        if now >= self.expires:
            self.closed = True
            raise ValueError("Run ownership expired before renewal commit")
        self.sequence = request.sequence
        self.challenge = challenge
        self.expires = now + self.grace_seconds
        return self.ack()

    def stop(self) -> None:
        self.closed = True
