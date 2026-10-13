# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native authenticated tool-policy administration and run evidence controls."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pyfly.container import rest_controller
from pyfly.web import Body, PathVar, get_mapping, post_mapping, put_mapping, request_mapping

from firefly_forge.tool_broker import ToolInvocationBroker, ToolPolicyInput


class ToolReconciliationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")


@rest_controller
@request_mapping("/api/v1")
class ToolController:
    def __init__(self, broker: ToolInvocationBroker) -> None:
        self.broker = broker

    @get_mapping("/tools/policy")
    def policy(self) -> dict[str, Any]:
        return self.broker.snapshot()

    @put_mapping("/tools/policy")
    def configure(self, body: Body[ToolPolicyInput]) -> dict[str, Any]:
        return self.broker.configure(body)

    @get_mapping("/runs/{id}/tool-invocations")
    async def invocations(self, id: PathVar[str]) -> list[dict[str, Any]]:
        return await self.broker.invocations(id)

    @post_mapping("/runs/{id}/tool-invocations/{correlation}/reconcile")
    async def reconcile(
        self, id: PathVar[str], correlation: PathVar[str], body: Body[ToolReconciliationInput]
    ) -> dict[str, Any]:
        return await self.broker.reconcile(id, correlation, body.request_id)
