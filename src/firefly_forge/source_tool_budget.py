# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Safe lifetime source-reader budgets and definite pre-effect refusals."""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firefly_forge.source_edit_models import READ_TOOLS

Data = dict[str, Any]


class BudgetCounter(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    used: int = Field(ge=0, le=10000)
    limit: int = Field(ge=1, le=100)
    remaining: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def exact_remaining(self) -> BudgetCounter:
        if self.remaining != max(0, self.limit - self.used):
            raise ValueError("A source budget counter requires its exact remaining count")
        return self


class SourceBudgetSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1]
    scope: Literal["run_lifetime"]
    total: BudgetCounter
    tools: dict[Literal["list_files", "read_file", "search_text"], BudgetCounter]


def source_budget_snapshot(rows: list[Any], settings: Data, *, per_tool: dict[str, int] | None = None) -> Data:
    if len(rows) > 10000:
        raise ValueError("The lifetime tool history exceeds its bounded budget projection")
    total = min(30, settings["calls_limit"])
    limits = {tool: min(10, settings["per_tool_limit"]) for tool in READ_TOOLS}
    if per_tool is not None:
        if set(per_tool) != set(READ_TOOLS) or any(type(n) is not int or not 1 <= n <= 30 for n in per_tool.values()):
            raise ValueError("Source-reader ceilings require an exact validated effective policy")
        limits = dict(per_tool)

    def counter(used: int, limit: int) -> Data:
        return {"used": used, "limit": limit, "remaining": max(0, limit - used)}

    return SourceBudgetSnapshot.model_validate(
        {
            "version": 1,
            "scope": "run_lifetime",
            "total": counter(len(rows), total),
            "tools": {tool: counter(sum(row.tool == tool for row in rows), limits[tool]) for tool in READ_TOOLS},
        }
    ).model_dump()


class ToolBudgetRefusal(ValueError):
    def __init__(self, tool: str, budget: Data) -> None:
        if tool not in READ_TOOLS:
            raise ValueError("A typed source refusal requires an admitted source reader")
        checked = SourceBudgetSnapshot.model_validate(budget)
        if set(checked.tools) != set(READ_TOOLS):
            raise ValueError("A source refusal requires every remaining reader capability")
        if (
            checked.total.remaining
            and checked.tools[cast(Literal["list_files", "read_file", "search_text"], tool)].remaining
        ):
            raise ValueError("A definite budget refusal requires an exhausted current ceiling")
        self.safe_result = {
            "version": 1,
            "status": "refused",
            "code": "tool_budget_exhausted",
            "effect_started": False,
            "retryable": False,
            "requested_tool": tool,
            "budget": checked.model_dump(),
        }
        super().__init__("The lifetime source-reader budget is exhausted; no tool effect started")
