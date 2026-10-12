# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Validated public requests for trusted factory operators."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

Provider = Literal["git", "github", "gitlab", "bitbucket", "azure", "gitea"]
Backend = Literal["pyfly", "larafly", "java", "rust", "dotnet"]
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


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_id: str = Field(min_length=1, max_length=100)
    brief: str = Field(min_length=1, max_length=40000)
    backend: Backend = "pyfly"
    frontend: Frontend = "react"
    agent: Literal["codex", "claude", "bridge", "agentic", "api"] = "codex"
    commands: list[Command] = Field(default_factory=list, max_length=20)
    publish: bool = False
    operation: Literal["build", "maintain", "test"] = "build"
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
