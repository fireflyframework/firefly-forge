# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Trusted-operator model profiles and task defaults with write-only credentials."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, put_mapping, request_mapping

from firefly_forge.configuration import SECRETS
from firefly_forge.model_profiles import ModelProfileInput, ModelProfiles
from firefly_forge.store import Store
from firefly_forge.task_routing import TaskRouting, TaskRoutingInput


@rest_controller
@request_mapping("/api/v1")
class ModelProfileController:
    def __init__(self, profiles: ModelProfiles, routing: TaskRouting, store: Store) -> None:
        self.profiles, self.routing, self.store = profiles, routing, store

    @get_mapping("/model-profiles")
    async def profiles_catalog(self) -> list[dict[str, Any]]:
        result = self.profiles.catalog()
        await self.store.admit_credentials(result)
        return result

    @put_mapping("/model-profiles/{id}")
    async def save_profile(self, id: PathVar[str], body: Body[ModelProfileInput]) -> dict[str, Any]:
        await self.store.admit_credentials(
            {
                "name": body.name,
                "description": body.description,
                "values": {key: value for key, value in body.values.items() if key not in SECRETS},
            }
        )
        public, digest = self.profiles.preview_save(id, body)
        await self.store.admit_credentials(public)
        return self.profiles.save(id, body, expected_digest=digest)

    @post_mapping("/model-profiles/{id}/test")
    async def test_profile(self, id: PathVar[str]) -> dict[str, Any]:
        return await self.profiles.test(id)

    @get_mapping("/task-routing")
    def task_routing(self) -> dict[str, Any]:
        return self.routing.current()

    @put_mapping("/task-routing")
    async def save_routing(self, body: Body[TaskRoutingInput]) -> dict[str, Any]:
        return await self.routing.save(body)
