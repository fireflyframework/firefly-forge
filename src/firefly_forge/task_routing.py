# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit task bindings without silent provider or model fallback."""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pyfly.container import service

from firefly_forge.model_profiles import TASK_PROVIDERS, ModelProfiles, profile_id, task_catalog
from firefly_forge.product_provenance import content_digest

Data = dict[str, Any]


class TaskRoutingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bindings: dict[str, str] = Field(default_factory=dict, max_length=8)

    @field_validator("bindings")
    @classmethod
    def actual_tasks(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) - TASK_PROVIDERS.keys():
            raise ValueError("Unknown model-routing task")
        for name in value.values():
            profile_id(name)
        return value


@service
class TaskRouting:
    def __init__(self, profiles: ModelProfiles) -> None:
        self.profiles, self.private = profiles, profiles.private
        self.path = self.private.directory / "task-routing.json"

    def current(self) -> Data:
        with self.private.lock:
            data = self.private._read(self.path) or {"revision": 0, "bindings": {}}
        return {
            **copy.deepcopy(data),
            "digest": content_digest(data),
            "tasks": task_catalog(),
            "mode": "explicit_task_profiles" if data["bindings"] else "legacy_provider_selection",
        }

    async def save(self, request: TaskRoutingInput) -> Data:
        for task, name in request.bindings.items():
            await self.profiles.validate_selection(name, task)
        with self.private.lock:
            previous = self.private._read(self.path)
            self.private._write(self.path, {"revision": previous.get("revision", 0) + 1, "bindings": request.bindings})
        return self.current()

    async def resolve(self, task: str, profile_override: str | None = None) -> Data | None:
        if task not in TASK_PROVIDERS:
            raise ValueError("Unknown model-routing task")
        current = self.current()
        name = profile_override if profile_override is not None else current["bindings"].get(task)
        if name is None:
            return None
        snapshot = await self.profiles.snapshot(name, task)
        return {**snapshot, "routing_revision": current["revision"], "routing_digest": current["digest"]}
