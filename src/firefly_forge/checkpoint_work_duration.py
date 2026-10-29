# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Opt-in owned work deadlines; legacy invocation bytes keep their finite contract."""

from __future__ import annotations

import time
from typing import Any

from firefly_forge.agent_checkpoint_models import (
    CheckpointPayloadV2,
    CheckpointPayloadV3,
    InvocationBinding,
    InvocationCheckpoint,
    InvocationContext,
    OwnedInvocationBindingV3,
)
from firefly_forge.work_duration_models import WorkDurationPolicyV2


def invocation_binding(value: dict[str, Any]) -> InvocationContext:
    if "protocol" in value:
        return OwnedInvocationBindingV3.model_validate(value)
    return InvocationBinding.model_validate(value)


def checkpoint_payload(value: dict[str, Any]) -> InvocationCheckpoint:
    if value.get("version") == 3:
        return CheckpointPayloadV3.model_validate(value)
    return CheckpointPayloadV2.model_validate(value)


def selected_work_duration(run: dict[str, Any]) -> WorkDurationPolicyV2 | None:
    selected = run.get("work_duration")
    frozen = (run.get("sandbox_snapshot") or {}).get("configuration", {}).get("work_duration")
    if selected is None and frozen is None:
        return None
    policy = WorkDurationPolicyV2.model_validate(selected)
    retained = WorkDurationPolicyV2.model_validate(frozen)
    if selected != policy.model_dump(mode="json") or frozen != retained.model_dump(mode="json") or policy != retained:
        raise ValueError("Checkpoint work duration must exactly match its frozen runner policy")
    return policy


def validate_binding_duration(run: dict[str, Any], binding: InvocationContext) -> None:
    policy = selected_work_duration(run)
    if isinstance(binding, OwnedInvocationBindingV3):
        if policy != binding.work_duration:
            raise ValueError("Owned checkpoint work duration changed")
    elif policy is not None:
        raise ValueError("A new work-duration policy cannot downgrade to a legacy checkpoint")


def binding_deadline(binding: InvocationContext) -> int | None:
    return binding.work_deadline_ms if isinstance(binding, OwnedInvocationBindingV3) else binding.deadline_ms


def binding_expired(binding: InvocationContext, *, now_ms: int | None = None) -> bool:
    deadline = binding_deadline(binding)
    return deadline is not None and deadline <= (time.time() * 1000 if now_ms is None else now_ms)


def binding_remaining(binding: InvocationContext, *, now_ms: int | None = None) -> float | None:
    deadline = binding_deadline(binding)
    if deadline is None:
        return None
    remaining = (deadline - (time.time() * 1000 if now_ms is None else now_ms)) / 1000
    if remaining <= 0:
        raise ValueError("Original checkpoint invocation deadline expired")
    return remaining


def same_owned_duration(previous: InvocationContext, current: InvocationContext) -> bool:
    if isinstance(previous, OwnedInvocationBindingV3) or isinstance(current, OwnedInvocationBindingV3):
        return (
            isinstance(previous, OwnedInvocationBindingV3)
            and isinstance(current, OwnedInvocationBindingV3)
            and previous.work_duration == current.work_duration
            and previous.work_started_at_ms == current.work_started_at_ms
            and previous.work_deadline_ms == current.work_deadline_ms
        )
    return True
