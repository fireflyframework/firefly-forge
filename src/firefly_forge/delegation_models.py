# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Versioned automatic factory teams and exact child-work intentions."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from firefly_forge.workspace_tools import _private_name

Identity = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class VersionedRecord(StrictRecord):
    version: Literal[1] = 1

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("The delegation protocol requires integer version 1")
        return value


class FamilyBudget(StrictRecord):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    requests: int | None = Field(gt=0)
    tool_calls: int | None = Field(ge=0)
    commands: int | None = Field(ge=0)
    broker_calls: int | None = Field(default=0, ge=0)


class FactoryTeamV1(StrictRecord):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    protocol: Literal["forge-factory-team-v1"] = "forge-factory-team-v1"
    automatic: Literal[True] = True
    budget: FamilyBudget
    broker_per_tool_limit: int | None = Field(default=10, gt=0)

    @field_validator("automatic", mode="before")
    @classmethod
    def exact_automatic(cls, value: object) -> object:
        if value is not True:
            raise ValueError("New factory teams use their approved automatic admission")
        return value


class DelegationStartInput(VersionedRecord):
    request_id: Identity
    task_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    brief: str = Field(min_length=1, max_length=40000)
    mode: Literal["analysis", "proposal"] = "analysis"
    write_paths: list[str] = Field(default_factory=list)

    @field_validator("brief")
    @classmethod
    def nonempty_brief(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Describe the delegated task")
        return value

    @field_validator("write_paths")
    @classmethod
    def public_source_paths(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Declare each source path once")
        for value in values:
            path = PurePosixPath(value)
            if (
                path.is_absolute()
                or not path.parts
                or ".." in path.parts
                or "\\" in value
                or "\x00" in value
                or path.as_posix() != value
                or any(_private_name(part) for part in path.parts)
            ):
                raise ValueError("Child proposals require public relative source paths")
        return values

    @model_validator(mode="after")
    def explicit_proposal_ownership(self) -> DelegationStartInput:
        if bool(self.write_paths) != (self.mode == "proposal"):
            raise ValueError("Only a source proposal declares its nonempty write ownership")
        return self


class ChildSelection(VersionedRecord):
    delegation_ids: list[Identity] = Field(min_length=1)

    @field_validator("delegation_ids")
    @classmethod
    def unique_children(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Select each child once")
        return values


class DelegationWaitInput(ChildSelection):
    request_id: Identity
    after_sequence: int = Field(default=0, ge=0)
    timeout_seconds: int | None = Field(default=None, gt=0)


class DelegationJoinInput(ChildSelection):
    request_id: Identity


class DelegationCancelInput(VersionedRecord):
    request_id: Identity
    delegation_id: Identity


class FamilyScheduling(VersionedRecord):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    waiting_parents: frozenset[Identity] = frozenset()
    ready_children: tuple[Identity, ...] = ()

    @model_validator(mode="after")
    def distinct_owned_states(self) -> FamilyScheduling:
        if len(self.ready_children) != len(set(self.ready_children)) or self.waiting_parents.intersection(
            self.ready_children
        ):
            raise ValueError("Factory scheduling requires distinct current child and waiting-parent states")
        return self
