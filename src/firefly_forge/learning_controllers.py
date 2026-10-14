# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Authenticated learning review and one-shot knowledge projection controls."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, put_mapping, request_mapping

from firefly_forge.learning import LearningLedger
from firefly_forge.learning_configuration import configure_learning, learning_snapshot
from firefly_forge.learning_models import LearningPolicy
from firefly_forge.learning_projection import LearningProjection
from firefly_forge.store import Store


class LearningAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class LearningInvalidation(LearningAction):
    reason: str = Field(min_length=1, max_length=2000)


class LearningObservation(LearningAction):
    source_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,200}$")


@rest_controller
@request_mapping("/api/v1")
class LearningController:
    def __init__(self, ledger: LearningLedger, projection: LearningProjection, store: Store) -> None:
        self.ledger, self.projection, self.store = ledger, projection, store

    @get_mapping("/learning/policy")
    def policy(self) -> dict[str, Any]:
        return learning_snapshot(self.store.configuration)

    @put_mapping("/learning/policy")
    def configure(self, body: Body[LearningPolicy]) -> dict[str, Any]:
        return configure_learning(self.store.configuration, body)

    @get_mapping("/repositories/{id}/learning")
    async def records(self, id: PathVar[str]) -> list[dict[str, Any]]:
        return await self.ledger.records(id)

    @post_mapping("/learning/records/{id}/invalidate")
    async def invalidate(self, id: PathVar[str], body: Body[LearningInvalidation]) -> dict[str, Any]:
        return await self.ledger.invalidate(id, reason=body.reason, request_id=body.request_id)

    @get_mapping("/learning/records/{id}/projection")
    async def projection_status(self, id: PathVar[str]) -> dict[str, Any]:
        return await self.projection.status(id)

    @post_mapping("/learning/records/{id}/projection/submit")
    async def submit(self, id: PathVar[str], body: Body[LearningAction]) -> dict[str, Any]:
        return await self.projection.submit(id, request_id=body.request_id)

    @post_mapping("/learning/records/{id}/projection/observe")
    async def observe(self, id: PathVar[str], body: Body[LearningObservation]) -> dict[str, Any]:
        return await self.projection.observe(id, source_id=body.source_id, request_id=body.request_id)
