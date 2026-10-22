# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Frozen input for the original routing tool, separate from API additions."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from firefly_forge.task_routing import TaskRoutingInput as NativeTaskRoutingInput


class TaskRoutingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bindings: dict[str, str] = Field(default_factory=dict, max_length=8)

    @field_validator("bindings")
    @classmethod
    def actual_tasks(cls, value: dict[str, str]) -> dict[str, str]:
        return NativeTaskRoutingInput.actual_tasks(value)
