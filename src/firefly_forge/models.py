# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Validated public requests for trusted factory operators."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from firefly_forge.agent_roles import ProjectRoles
from firefly_forge.runner_factory_models import FactoryPoolSelection
from firefly_forge.sandbox_models import SandboxInput

Provider = Literal["git", "github", "gitlab", "bitbucket", "azure", "gitea"]
Backend = Literal["pyfly", "larafly", "java", "rust", "dotnet", "none"]
Frontend = Literal["react", "angular", "nextjs", "vue", "none"]


class RepositoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2048)
    provider: Provider = "git"
    default_branch: str = Field(default="main", min_length=1, max_length=200)
    connection_provider: Literal["github", "gitlab", "bitbucket"] | None = None
    remote_repository_id: str | None = Field(default=None, max_length=200)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if value.startswith("/"):
            return value
        if re.fullmatch(r"git@[a-zA-Z0-9.-]+:[a-zA-Z0-9_./-]+", value):
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "ssh"} or not parsed.hostname or parsed.password:
            raise ValueError("Use an HTTPS/SSH Git URL without embedded credentials, or an absolute local path")
        if parsed.scheme == "https" and parsed.username:
            raise ValueError("Configure credentials through the Git credential helper")
        if any(char in value for char in "\r\n\x00"):
            raise ValueError("Invalid Git URL")
        return value

    @field_validator("default_branch")
    @classmethod
    def validate_branch(cls, value: str) -> str:
        if value.startswith("-") or any(
            item in value for item in ["..", "@{", "\\", " ", "~", "^", ":", "?", "*", "["]
        ):
            raise ValueError("Invalid branch name")
        if any(ord(char) < 32 for char in value):
            raise ValueError("Invalid branch name")
        return value


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    argv: list[str] = Field(min_length=1, max_length=64)
    timeout: int = Field(default=300, ge=1, le=3600)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: list[str]) -> list[str]:
        if any(not part or "\x00" in part or len(part) > 8192 for part in value):
            raise ValueError("Command arguments must be nonempty strings")
        return value


class AssuranceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_agent: Literal["codex", "claude", "bridge", "agentic"] | None = None
    review_model_profile_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_id: str = Field(min_length=1, max_length=100)
    project_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    component_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    brief: str = Field(min_length=1, max_length=40000)
    backend: Backend = "pyfly"
    frontend: Frontend = "react"
    agent: Literal["codex", "claude", "bridge", "agentic", "api"] = "codex"
    model_profile_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,63}$")
    commands: list[Command] = Field(default_factory=list, max_length=20)
    publish: bool = False
    operation: Literal["build", "maintain", "test"] = "build"
    assurance: AssuranceInput = Field(default_factory=AssuranceInput)
    roles: ProjectRoles | None = None
    sandbox: FactoryPoolSelection | SandboxInput | None = None
    product_definition: dict[str, object] | None = None
    issue_context: dict[str, object] | None = None
    skills: list[str] = Field(default_factory=lambda: ["implementation", "verification", "security"], max_length=100)

    @field_validator("skills")
    @classmethod
    def known_skills(cls, value: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", identifier) for identifier in value):
            raise ValueError("Invalid policy skill identifier")
        return value

    @field_validator("brief")
    @classmethod
    def nonempty_brief(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Describe the product or change you want to build")
        return value.strip()

    @model_validator(mode="after")
    def bounded_customer_context(self) -> RunInput:
        import hashlib
        import json

        from firefly_forge.collaboration import ProductDefinition

        if bool(self.project_id) != bool(self.component_id):
            raise ValueError("Choose both a product project and one of its components")
        if self.backend == self.frontend == "none":
            raise ValueError("Choose a backend or frontend for this component")

        if self.product_definition is not None:
            snapshot = self.product_definition
            required = {"repository_id", "revision", "definition", "digest", "created_at"}
            if (
                not required.issubset(snapshot)
                or set(snapshot) - (required | {"origin"})
                or (not self.project_id and snapshot["repository_id"] != self.repository_id)
            ):
                raise ValueError("Product snapshot must belong to this repository and include its frozen revision")
            revision = snapshot["revision"]
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                raise ValueError("Product revision must be a positive integer")
            if not isinstance(snapshot["digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", snapshot["digest"]):
                raise ValueError("Product snapshot requires its SHA256 digest")
            definition = ProductDefinition.model_validate(snapshot["definition"])
            architecture = definition.architecture
            if architecture is not None:
                if not architecture.reviewed:
                    raise ValueError("Product architecture must be reviewed before a run")
                for field in ("backend", "frontend"):
                    selected = getattr(architecture, field)
                    if not self.project_id and field in self.model_fields_set and getattr(self, field) != selected:
                        raise ValueError("Selected stack conflicts with the frozen product architecture")
                    if not self.project_id:
                        setattr(self, field, selected)
            signed = {key: value for key, value in snapshot.items() if key != "digest"}
            actual = hashlib.sha256(json.dumps(signed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if actual != snapshot["digest"]:
                raise ValueError("Product snapshot digest does not match its frozen contents")
            if len(json.dumps(snapshot)) > 85000:
                raise ValueError("Product snapshot exceeds 85 KB")
        if self.issue_context is not None:
            if len(json.dumps(self.issue_context)) > 4000:
                raise ValueError("Issue context exceeds 4 KB")
            required = {"provider", "receipt_id", "actor", "kind", "number"}
            if not required.issubset(self.issue_context) or set(self.issue_context) - required - {"title", "url"}:
                raise ValueError("Issue context must contain its signed receipt and issue identity")
            context = self.issue_context
            if context["provider"] not in {"github", "gitlab", "bitbucket"} or context["kind"] not in {
                "issue",
                "pull_request",
                "merge_request",
            }:
                raise ValueError("Unknown issue provider or conversation kind")
            number = context["number"]
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                raise ValueError("Issue number must be a positive integer")
            if not isinstance(context["receipt_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", context["receipt_id"]):
                raise ValueError("Issue context must identify its delivery receipt")
            if not isinstance(context["actor"], str) or len(context["actor"]) > 200:
                raise ValueError("Invalid issue actor identifier")
            if "title" in context and (not isinstance(context["title"], str) or len(context["title"]) > 250):
                raise ValueError("Issue title exceeds 250 characters")
            if context.get("url"):
                if not isinstance(context["url"], str) or len(context["url"]) > 2000:
                    raise ValueError("Invalid issue URL")
                url = urlsplit(context["url"])
                if (
                    url.scheme != "https"
                    or not url.hostname
                    or url.username
                    or url.password
                    or url.query
                    or url.fragment
                ):
                    raise ValueError("Issue URL must be HTTPS without credentials or query parameters")
        return self
