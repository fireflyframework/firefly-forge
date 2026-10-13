# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Validated data-only policy catalogs from pinned Git revisions."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pyfly.container import service

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.models import Command, RepositoryInput
from firefly_forge.process import checked
from firefly_forge.skills import SKILLS


class PolicyCommand(Command):
    backends: list[Literal["pyfly", "larafly", "java", "rust", "dotnet"]] = Field(min_length=1, max_length=5)


class SkillProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    path: str = Field(min_length=1, max_length=300)
    source_url: str = Field(min_length=1, max_length=2048)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    license: Literal["MIT", "Apache-2.0"]
    license_path: str = Field(min_length=1, max_length=300)
    license_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    license_text: str = Field(min_length=1, max_length=20000)

    @model_validator(mode="after")
    def pinned_source(self) -> SkillProvenance:
        if self.source_url != f"https://github.com/{self.repository}/blob/{self.revision}/{self.path}":
            raise ValueError("Community source URL must identify its exact pinned file")
        if any(Path(path).is_absolute() or ".." in Path(path).parts for path in (self.path, self.license_path)):
            raise ValueError("Community source paths must be relative")
        if hashlib.sha256(self.license_text.encode()).hexdigest() != self.license_sha256:
            raise ValueError("Community license hash mismatch")
        return self


class SkillPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    category: str = Field(min_length=1, max_length=80)
    kind: Literal["recommendation", "quality_gate"] = "recommendation"
    default: bool = False
    instructions: str = Field(min_length=1, max_length=16000)
    commands: list[PolicyCommand] = Field(default_factory=list, max_length=10)
    adaptation: Literal["instruction-only"] | None = None
    adaptation_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    provenance: SkillProvenance | None = None

    @model_validator(mode="after")
    def meaningful_gate(self) -> SkillPolicy:
        metadata = [self.adaptation, self.adaptation_sha256, self.provenance]
        if any(value is not None for value in metadata):
            if any(value is None for value in metadata):
                raise ValueError("Community adaptation requires complete pinned provenance")
            if hashlib.sha256(self.instructions.encode()).hexdigest() != self.adaptation_sha256:
                raise ValueError("Community adaptation hash mismatch")
        if self.kind == "quality_gate" and not self.commands:
            raise ValueError("Quality gates require executable commands")
        if self.kind == "recommendation" and self.commands:
            raise ValueError("Recommendation policies cannot include commands")
        return self


class CatalogDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, max_length=80)
    skills: list[SkillPolicy] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_ids(self) -> CatalogDocument:
        if len({skill.id for skill in self.skills}) != len(self.skills):
            raise ValueError("Policy IDs must be unique")
        return self


class ProjectPolicies(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skills: list[str] = Field(default_factory=list, max_length=100)


class PolicySync(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_url: str = Field(min_length=1, max_length=2048)
    ref: str = Field(pattern=r"^[a-f0-9]{40}$")
    catalog_path: str = "catalog.json"

    @field_validator("repository_url")
    @classmethod
    def safe_repository(cls, value: str) -> str:
        return RepositoryInput(name="central policies", url=value).url

    @field_validator("catalog_path")
    @classmethod
    def safe_catalog_path(cls, value: str) -> str:
        path = Path(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not value.endswith(".json")
            or len(value) > 300
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("Use a relative JSON catalog path without traversal")
        return value


@service
class PolicyCatalog:
    def __init__(self, directory: Path) -> None:
        self.private = ConfigurationStore(directory)
        self.path = self.private.directory / "policies.json"

    @staticmethod
    def validate(value: Any) -> dict[str, Any]:
        return CatalogDocument.model_validate(value).model_dump()

    @staticmethod
    def _digest(document: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def current(self) -> dict[str, Any]:
        active = self.private._read(self.path)
        if active:
            document = self.validate(active["document"])
            return {
                "revision": active["revision"],
                "source": active["source"],
                "digest": self._digest(document),
                "version": document["version"],
                "skills": document["skills"],
            }
        external = os.environ.get("FORGE_POLICY_CATALOG")
        if external:
            try:
                fd = os.open(external, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd) as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 1_000_000:
                        raise ValueError("Policy catalog must be a JSON file below 1 MB")
                    document = self.validate(json.load(source))
                digest = self._digest(document)
                return {
                    "revision": digest,
                    "source": {"kind": "operator_file"},
                    "digest": digest,
                    "version": document["version"],
                    "skills": document["skills"],
                }
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("Configured policy catalog could not be loaded") from exc
        document = self.validate(
            {
                "version": "1.1.0",
                "skills": [{**item, "category": "sdlc", "kind": "recommendation", "commands": []} for item in SKILLS],
            }
        )
        return {
            "revision": "builtin-2",
            "source": {"kind": "builtin"},
            "digest": self._digest(document),
            "version": document["version"],
            "skills": document["skills"],
        }

    def selection(self, skills: list[str] | None = None) -> dict[str, Any]:
        current = self.current()
        if skills is None:
            skills = [item["id"] for item in current["skills"] if item["default"]]
        known = {item["id"] for item in current["skills"]}
        if set(skills) - known or len(set(skills)) != len(skills):
            raise ValueError("Unknown or duplicate policy skill selection")
        return {**current, "skills": [item for item in current["skills"] if item["id"] in skills]}

    async def sync(self, request: PolicySync) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="policy-sync-", dir=self.private.directory) as temporary:
            workspace = Path(temporary)
            await checked(["git", "init", "--quiet"], workspace)
            await checked(["git", "remote", "add", "origin", request.repository_url], workspace)
            await checked(["git", "fetch", "--depth", "1", "origin", request.ref], workspace, timeout=120)
            revision = await checked(["git", "rev-parse", "FETCH_HEAD"], workspace)
            if revision != request.ref:
                raise ValueError("Central policy revision did not match the requested commit")
            data = await checked(["git", "show", f"{revision}:{request.catalog_path}"], workspace)
            if len(data.encode()) > 1_000_000:
                raise ValueError("Policy catalog exceeds 1 MB")
            document = self.validate(json.loads(data))
        with self.private.lock:
            self.private._write(
                self.path,
                {
                    "revision": revision,
                    "source": {
                        "kind": "git",
                        "repository_url": request.repository_url,
                        "catalog_path": request.catalog_path,
                    },
                    "document": document,
                },
            )
        return self.current()


def inject_policy_snapshot(brief: str, snapshot: dict[str, Any]) -> str:
    skills = snapshot.get("skills", [])
    if not skills:
        return brief
    return (
        brief
        + "\n\nSelected reusable skills:\n"
        + "\n\n".join(f"[{item['id']}] {item['instructions']}" for item in skills)
    )


def policy_commands(snapshot: dict[str, Any], backend: str) -> list[Command]:
    commands = []
    for skill in snapshot.get("skills", []):
        if skill["kind"] == "quality_gate":
            for command in skill["commands"]:
                if backend in command["backends"]:
                    commands.append(Command(argv=command["argv"], timeout=command["timeout"]))
    return commands
