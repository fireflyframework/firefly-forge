# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit native administrator policy for finite source-only allowances."""

from typing import Any

from pyfly.container import rest_controller
from pyfly.web import Body, get_mapping, put_mapping, request_mapping

from firefly_forge.source_budget_allowances import SourceBudgetPolicies, SourceBudgetPolicy


@rest_controller
@request_mapping("/api/v1/source-budget/policy")
class SourceBudgetPolicyController:
    def __init__(self, policies: SourceBudgetPolicies) -> None:
        self.policies = policies

    @get_mapping("")
    async def get(self) -> dict[str, Any]:
        return await self.policies.get()

    @put_mapping("")
    async def configure(self, body: Body[SourceBudgetPolicy]) -> dict[str, Any]:
        return await self.policies.configure(body)
