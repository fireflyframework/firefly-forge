# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A product's repository topology and bounded component execution contracts."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from firefly_forge.agent_roles import ProjectRoles
from firefly_forge.delegation_models import FactoryTeamV1
from firefly_forge.models import Backend, Command, Frontend


def component_path(value: str) -> str:
    path = PurePosixPath(value)
    if value == ".":
        return value
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value or len(value) > 200:
        raise ValueError("Use a normalized relative component path")
    if any(ord(char) < 32 for char in value) or "\\" in value or any(part.startswith(".") for part in path.parts):
        raise ValueError("Component paths must not enter private directories")
    return value


class ProjectComponent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    repository_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    path: str = "."
    kind: Literal["api", "web", "worker", "library", "fullstack"] = "api"
    backend: Backend = "pyfly"
    frontend: Frontend = "none"
    verification: list[Command] = Field(default_factory=list, max_length=20)
    depends_on: list[str] = Field(default_factory=list)
    responsibility: str = Field(min_length=1, max_length=2000)

    @field_validator("path")
    @classmethod
    def relative_root(cls, value: str) -> str:
        return component_path(value)

    @model_validator(mode="after")
    def stack(self) -> ProjectComponent:
        if self.kind == "web" and (self.backend != "none" or self.frontend == "none"):
            raise ValueError("A standalone web component requires a frontend and no backend")
        if self.kind in {"api", "worker", "library"} and (self.backend == "none" or self.frontend != "none"):
            raise ValueError("Service and library components require a backend and no browser frontend")
        return self


class ProjectDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=4000)
    topology: Literal["monorepo", "multi_repo"]
    product_repository_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    components: list[ProjectComponent] = Field(min_length=1)
    owners: list[str] = Field(min_length=1)
    roles: ProjectRoles | None = None
    factory_team: FactoryTeamV1 | None = None

    @model_serializer(mode="wrap")
    def retained_project_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        value: dict[str, object] = handler(self)
        if value.get("factory_team") is None:
            value.pop("factory_team", None)
        return value

    @model_validator(mode="after")
    def coherent_topology(self) -> ProjectDefinition:
        components = {component.id: component for component in self.components}
        if len(components) != len(self.components):
            raise ValueError("Component identifiers must be unique")
        repositories = {component.repository_id for component in self.components}
        if self.product_repository_id is None:
            self.product_repository_id = self.components[0].repository_id
        if self.product_repository_id not in repositories:
            raise ValueError("The product definition and design source must be a member repository")
        if self.topology == "monorepo" and len(repositories) != 1:
            raise ValueError("A monorepo's components share one registered repository")
        if self.topology == "multi_repo" and len(repositories) < 2:
            raise ValueError("A multi-repository product requires at least two registered repositories")
        # Segment order keeps a parent adjacent to its first descendant.
        roots = sorted((component.repository_id, PurePosixPath(component.path).parts) for component in self.components)
        for (first_repository, first), (second_repository, second) in zip(roots, roots[1:], strict=False):
            if first_repository == second_repository and second[: len(first)] == first:
                raise ValueError("Components in one repository require distinct nonoverlapping roots")
        visiting: set[str] = set()
        visited: set[str] = set()
        for component in self.components:
            if component.id in visited:
                continue
            visiting.add(component.id)
            stack: list[tuple[str, Iterator[str]]] = [(component.id, iter(component.depends_on))]
            while stack:
                current, dependencies = stack[-1]
                dependency = next(dependencies, None)
                if dependency is None:
                    visiting.remove(current)
                    visited.add(current)
                    stack.pop()
                    continue
                if dependency not in components:
                    raise ValueError("Delivery dependencies must reference this project's components")
                if dependency in visiting:
                    raise ValueError("Component delivery dependencies must not form cycles")
                if dependency not in visited:
                    visiting.add(dependency)
                    stack.append((dependency, iter(components[dependency].depends_on)))
        return self
