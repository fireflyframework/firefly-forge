# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit work deadlines for new policies; legacy captures keep their own contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkDurationPolicyV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol: Literal["forge-work-duration-v2"] = "forge-work-duration-v2"
    timeout_seconds: int | None = Field(default=None, gt=0, strict=True)


def effective_work_timeout(*timeouts: int | None) -> int | None:
    explicit = []
    for timeout in timeouts:
        if timeout is not None:
            if type(timeout) is not int or timeout < 1:
                raise ValueError("An explicit work deadline must be a positive integer")
            explicit.append(timeout)
    return min(explicit) if explicit else None
