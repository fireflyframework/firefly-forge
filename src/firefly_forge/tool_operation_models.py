# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""New operation work deadlines are independent from renewable ownership grace."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from firefly_forge.bridge_completion_models import Digest, Identifier, StrictRecord
from firefly_forge.checkpoint_work_duration import selected_work_duration
from firefly_forge.runner_pool_models import FactoryFence
from firefly_forge.work_duration_models import WorkDurationPolicyV2


class OwnedToolOperationV2(StrictRecord):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[2] = 2
    protocol: Literal["forge-owned-tool-operation-v2"] = "forge-owned-tool-operation-v2"
    generation: Identifier
    task_key: Identifier
    intent_digest: Digest
    task_id: str = Field(min_length=1)
    attempt: int = Field(gt=0)
    factory_fence: FactoryFence
    run_cut: Digest
    work_duration: WorkDurationPolicyV2
    work_started_at_ms: int = Field(gt=0)
    work_deadline_ms: int | None = Field(gt=0)
    owner_grace_seconds: int = Field(default=30, gt=0)

    @model_validator(mode="after")
    def exact_optional_work_deadline(self) -> OwnedToolOperationV2:
        duration = self.work_duration.timeout_seconds
        expected = None if duration is None else self.work_started_at_ms + duration * 1000
        if self.work_deadline_ms != expected:
            raise ValueError("Tool work deadline must retain its original chosen start and policy")
        return self


def operation_policy(run: dict[str, Any], *, source_read: bool = False) -> WorkDurationPolicyV2 | None:
    policy = selected_work_duration(run)
    return None if source_read else policy
