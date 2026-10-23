# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit source-reading authority, distinct from execution and external MCP grants."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from firefly_forge.workspace_reconciliation_models import WorkspaceReconciliationInput

PROTOCOL = "native-source-edit-continuation-v1"
READ_TOOLS = ("list_files", "read_file", "search_text")


class SourceEditContinuationInput(WorkspaceReconciliationInput):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    acknowledge_existing_read_scope: Literal[True]
    acknowledge_no_commands: Literal[True]

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("Select source-edit continuation version 1")
        return value

    @field_validator("acknowledge_existing_read_scope", "acknowledge_no_commands", mode="before")
    @classmethod
    def explicit_scope(cls, value: Any) -> bool:
        if value is not True:
            raise ValueError("Explicitly acknowledge existing source readers and no commands")
        return value


class SourceReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    tool: Literal["list_files", "read_file", "search_text"]
    path: str = Field(default="", max_length=1000)
    text: str = Field(default="", max_length=500)
    max_results: int = Field(default=50, ge=1, le=100)

    @model_validator(mode="after")
    def exact_tool_arguments(self) -> "SourceReadRequest":
        allowed = {"request_id", "tool"} | {
            "list_files": set(),
            "read_file": {"path"},
            "search_text": {"text", "path", "max_results"},
        }[self.tool]
        if self.model_fields_set - allowed or (self.tool == "read_file" and not self.path):
            raise ValueError("Select only the exact source reader arguments")
        if self.tool == "search_text" and not self.text:
            raise ValueError("Search text must be nonempty")
        return self
