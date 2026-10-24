# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Editable role instructions with fixed, honest factory execution bindings."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pyfly.container import service

from firefly_forge.community_skills import community_catalog
from firefly_forge.configuration import ConfigurationStore
from firefly_forge.prompts import prompt_text
from firefly_forge.secrets import contains_known_credential
from firefly_forge.skills import SKILLS

Data = dict[str, Any]
ROLE_IDS = {"product-manager", "tech-lead", "architect", "implementer", "qa", "security", "docs"}
BINDINGS: dict[str, tuple[Literal["deterministic", "coding_provider"], list[str], Literal["factory", "run"]]] = {
    "product-manager": ("deterministic", ["prepare"], "factory"),
    "tech-lead": ("deterministic", ["prepare", "verify", "checkpoint"], "factory"),
    "architect": ("deterministic", ["prepare"], "factory"),
    "implementer": ("coding_provider", ["implement"], "run"),
    "qa": ("deterministic", ["verify"], "factory"),
    "security": ("deterministic", ["prepare", "verify", "checkpoint", "publication"], "factory"),
    "docs": ("coding_provider", ["implement"], "run"),
}
MANDATORY = ROLE_IDS - {"docs"}


def role_digest(value: Data) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class RoleDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    execution_mode: Literal["deterministic", "coding_provider"]
    provider_binding: Literal["factory", "run"]
    stages: list[str] = Field(min_length=1, max_length=6)
    responsibilities: list[str] = Field(min_length=1, max_length=12)
    skills: list[str] = Field(default_factory=list, max_length=30)
    allowed_tools: list[str] = Field(default_factory=list, max_length=30)
    inputs: list[str] = Field(min_length=1, max_length=12)
    outputs: list[str] = Field(min_length=1, max_length=12)
    instructions: str = Field(min_length=1, max_length=8000)
    feedback: str = Field(min_length=1, max_length=2000)
    escalation: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def actual_binding(self) -> RoleDefinition:
        if self.id not in BINDINGS or (self.execution_mode, self.stages, self.provider_binding) != BINDINGS[self.id]:
            raise ValueError("Role binding must match an implemented factory execution stage")
        if self.id in MANDATORY and not self.enabled:
            raise ValueError("A mandatory factory role cannot be disabled")
        if self.execution_mode == "deterministic" and self.allowed_tools:
            raise ValueError("Deterministic roles use factory gates, not worker tool grants")
        for values in (self.responsibilities, self.skills, self.allowed_tools, self.inputs, self.outputs):
            if len(set(values)) != len(values) or any(not value.strip() or len(value) > 2000 for value in values):
                raise ValueError("Role entries must be unique, bounded and nonempty")
        return self


class RoleDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, max_length=80)
    max_requests: int = Field(default=3, ge=1, le=3)
    roles: list[RoleDefinition] = Field(min_length=7, max_length=7)

    @model_validator(mode="after")
    def complete_roles(self) -> RoleDocument:
        if {role.id for role in self.roles} != ROLE_IDS:
            raise ValueError("The registry requires each supported role exactly once")
        return self


class ProjectRoles(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disabled: list[str] = Field(default_factory=list, max_length=7)
    enabled: list[str] = Field(default_factory=list, max_length=7)
    skills: dict[str, list[str]] = Field(default_factory=dict, max_length=7)
    allowed_tools: dict[str, list[str]] = Field(default_factory=dict, max_length=7)


def _default_document() -> RoleDocument:
    specifications = [
        (
            "product-manager",
            "Product manager",
            ["Freeze approved customer outcomes and functional traceability"],
            ["thinking"],
            ["customer definition", "journey-feature behaviors", "review decisions"],
            ["frozen product contract"],
            prompt_text("roles/product-manager"),
        ),
        (
            "tech-lead",
            "Technical lead",
            ["Manage bounded repair and delivery evidence"],
            ["evidence-first"],
            ["development plan", "check outcomes", "worker feedback"],
            ["stage decisions", "delivery checkpoint"],
            prompt_text("roles/tech-lead"),
        ),
        (
            "architect",
            "Architect",
            ["Retain reviewed component and interface boundaries"],
            ["thinking"],
            ["reviewed architecture", "knowledge references", "product constraints"],
            ["frozen architectural handoff"],
            prompt_text("roles/architect"),
        ),
        (
            "implementer",
            "Implementation worker",
            ["Implement customer behavior and regression tests"],
            ["implementation", "test-driven-development", "systematic-debugging", "security"],
            ["frozen product and feature contracts", "reviewed design references", "workspace source"],
            ["changed source", "behavioral tests", "typed worker feedback"],
            prompt_text("roles/implementer"),
        ),
        (
            "qa",
            "Quality assurance",
            ["Run the configured executable checks"],
            ["verification", "evidence-first"],
            ["actual source tree", "expected behaviors", "configured commands"],
            ["real check outcomes", "regression evidence"],
            prompt_text("roles/qa"),
        ),
        (
            "security",
            "Security boundaries",
            ["Enforce credentials, repository scope and verified publication guards"],
            ["security"],
            ["authorized repository", "source snapshot", "security requirements"],
            ["source and scope gate outcomes"],
            prompt_text("roles/security"),
        ),
        (
            "docs",
            "Documentation guidance",
            ["Write reproducible customer and operator instructions"],
            ["documentation", "humanizer"],
            ["actual product behavior", "check evidence", "operational limits"],
            ["installation and usage documentation"],
            prompt_text("roles/docs"),
        ),
    ]
    roles = []
    for ident, name, responsibilities, skills, inputs, outputs, instructions in specifications:
        mode, stages, binding = BINDINGS[ident]
        roles.append(
            RoleDefinition(
                id=ident,
                name=name,
                enabled=ident != "docs",
                execution_mode=mode,
                provider_binding=binding,
                stages=stages,
                responsibilities=responsibilities,
                skills=skills,
                allowed_tools=[
                    "list_files",
                    "read_file",
                    "write_file",
                    "search_text",
                    "edit_lines",
                    "git_diff",
                    "knowledge_lookup",
                ]
                if mode == "coding_provider"
                else [],
                inputs=inputs,
                outputs=outputs,
                instructions=instructions,
                feedback="Record actual outcomes and evidence using the typed worker feedback contract.",
                escalation="Persist a bounded clarification or blocker before pausing; never infer approval.",
            )
        )
    return RoleDocument(version="1.1.0", roles=roles)


@service
class RoleRegistry:
    def __init__(self, directory: Path) -> None:
        self.private = ConfigurationStore(directory)
        self.path = self.private.directory / "roles.json"
        self.credential_registry: Callable[[], dict[str, str]] = dict

    def current(self) -> Data:
        with self.private.lock:
            value = self.private._read(self.path)
        document = RoleDocument.model_validate(value["document"]) if value else _default_document()
        data = document.model_dump(mode="json")
        self._reject_credentials(data)
        return {"revision": value.get("revision", "builtin-2"), "digest": role_digest(data), "document": data}

    def save(self, value: RoleDocument | Data) -> Data:
        document = RoleDocument.model_validate(value).model_dump(mode="json")
        self._reject_credentials(document)
        self._validate_skills(document["roles"])
        self._validate_tools(document["roles"], self.tools())
        digest = role_digest(document)
        with self.private.lock:
            self.private._write(self.path, {"revision": digest, "document": document})
        return self.current()

    def _reject_credentials(self, document: Data) -> None:
        if contains_known_credential(document, self.credential_registry()):
            raise ValueError("Remove configured credentials from role instructions and feedback before saving")

    def _skills(self, policy_snapshot: Data | None = None) -> list[Data]:
        entries = {item["id"]: copy.deepcopy(item) for item in [*SKILLS, *community_catalog()]}
        if policy_snapshot is None:
            from firefly_forge.policies import PolicyCatalog

            policy_snapshot = PolicyCatalog(self.private.directory.parent).current()
        for item in policy_snapshot.get("skills", []):
            entries[item["id"]] = copy.deepcopy(item)
        return list(entries.values())

    def _validate_skills(self, roles: list[Data], skills: list[Data] | None = None) -> None:
        known = {item["id"] for item in skills or self._skills()}
        if any(set(role["skills"]) - known for role in roles):
            raise ValueError("Unknown role skill selection")

    @staticmethod
    def tools() -> list[Data]:
        try:
            tool_module = import_module("firefly_forge.workspace_tools")
        except ModuleNotFoundError as exc:
            if exc.name != "firefly_forge.workspace_tools":
                raise
            return [
                {
                    "id": ident,
                    "availability": "available",
                    "providers": ["agentic", "bridge"],
                    "reason": "Confined native workspace tool",
                }
                for ident in ["list_files", "read_file", "write_file"]
            ]
        result: list[Data] = tool_module.tool_catalog()
        return result

    @staticmethod
    def _validate_tools(roles: list[Data], tools: list[Data]) -> None:
        known = {tool["id"] for tool in tools}
        if any(set(role["allowed_tools"]) - known for role in roles):
            raise ValueError("Unknown role tool selection; select actual registered tool IDs")

    def snapshot(
        self,
        overrides: ProjectRoles | None = None,
        policy_snapshot: Data | None = None,
        *,
        tool_catalog: list[Data] | None = None,
    ) -> Data:
        active = self.current()
        document = copy.deepcopy(active["document"])
        overrides = overrides or ProjectRoles()
        if (
            set(overrides.disabled + overrides.enabled) - ROLE_IDS
            or set(overrides.skills) - ROLE_IDS
            or set(overrides.allowed_tools) - ROLE_IDS
        ):
            raise ValueError("Unknown project role override")
        if set(overrides.disabled) & set(overrides.enabled):
            raise ValueError("A role cannot be enabled and disabled together")
        if set(overrides.disabled) & MANDATORY:
            raise ValueError("A mandatory factory role cannot be disabled")
        for role in document["roles"]:
            role["enabled"] = (role["enabled"] or role["id"] in overrides.enabled) and role[
                "id"
            ] not in overrides.disabled
            if role["id"] in overrides.skills:
                role["skills"] = overrides.skills[role["id"]]
            if role["id"] in overrides.allowed_tools:
                role["allowed_tools"] = overrides.allowed_tools[role["id"]]
        document = RoleDocument.model_validate(document).model_dump(mode="json")
        available = self._skills(policy_snapshot)
        self._validate_skills(document["roles"], available)
        tools = tool_catalog if tool_catalog is not None else self.tools()
        self._validate_tools(document["roles"], tools)
        selected = {skill for role in document["roles"] if role["enabled"] for skill in role["skills"]}
        payload = {
            **document,
            "revision": active["revision"],
            "tool_catalog": copy.deepcopy(tools),
            "cli_tools": "Official CLI tool authority is separate; native role grants are advisory for Codex/Claude.",
            "skills": [item for item in available if item["id"] in selected],
        }
        return {**payload, "digest": role_digest(payload)}


def roles_for_stage(snapshot: Data, task_id: str) -> list[Data]:
    payload = {key: value for key, value in snapshot.items() if key != "digest"}
    if snapshot.get("digest") != role_digest(payload):
        raise ValueError("Role snapshot digest mismatch")
    return [copy.deepcopy(role) for role in snapshot["roles"] if role["enabled"] and task_id in role["stages"]]


def stage_allowed_tools(snapshot: Data, stage: str, provider: str) -> set[str] | None:
    if provider not in {"agentic", "bridge"}:
        return None
    requested = {tool for role in roles_for_stage(snapshot, stage) for tool in role["allowed_tools"]}
    available = {
        tool["id"]
        for tool in snapshot.get("tool_catalog", [])
        if tool["availability"] in {"available", "worker_unverified"} and provider in tool["providers"]
    }
    return requested & available


def inject_role_snapshot(brief: str, snapshot: Data, stage: str = "implement") -> str:
    payload = {key: value for key, value in snapshot.items() if key != "digest"}
    if snapshot.get("digest") != role_digest(payload):
        raise ValueError("Role snapshot digest mismatch")
    selected = roles_for_stage(snapshot, stage)
    active = {skill for role in selected for skill in role["skills"]}
    lines = [
        prompt_text(
            "roles/entry",
            role_id=f"{role['id']}",
            execution_mode=f"{role['execution_mode']}",
            instructions=f"{role['instructions']}",
        )
        for role in selected
    ]
    lines.extend(
        prompt_text("skills/entry", skill_id=f"{skill['id']}", instructions=f"{skill['instructions']}")
        for skill in snapshot["skills"]
        if skill["id"] in active
    )
    return prompt_text("roles/frozen", brief=brief, instructions="\n\n".join(lines))


class WorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
    kind: Literal["product_definition", "architecture", "design", "provider", "verification", "approval", "other"]
    question: str = Field(min_length=1, max_length=4000)
    context: str = Field(min_length=1, max_length=4000)
    choices: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("choices")
    @classmethod
    def bounded_choices(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 1000 for value in values):
            raise ValueError("Request choices must be bounded and nonempty")
        return values


class WorkerFeedback(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "description": (
                "For blocked or clarification disposition, supply exactly one real typed request naming the "
                "unresolved question and its context; do not invent a question to satisfy this schema. "
                "For completed or failed disposition, request must be null or omitted. "
                "A failed report is not successful implementation or permission to retry."
            ),
            "allOf": [
                {
                    "if": {
                        "properties": {"disposition": {"enum": ["blocked", "clarification"]}},
                        "required": ["disposition"],
                    },
                    "then": {"required": ["request"], "properties": {"request": {"type": "object"}}},
                    "else": {"properties": {"request": {"type": "null"}}},
                }
            ],
        },
    )
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
    role_id: str
    task_id: Literal["prepare", "implement", "verify", "checkpoint", "review", "publication"]
    attempt: int = Field(ge=1, le=4, strict=True)
    disposition: Literal["completed", "blocked", "clarification", "failed"]
    summary: str = Field(min_length=1, max_length=8000)
    evidence_paths: list[str] = Field(default_factory=list, max_length=100)
    request: WorkerRequest | None = None

    @model_validator(mode="after")
    def request_contract(self) -> WorkerFeedback:
        if self.role_id not in ROLE_IDS:
            raise ValueError("Unknown worker role")
        if (self.disposition in {"blocked", "clarification"}) != (self.request is not None):
            raise ValueError("Blocked or clarification feedback requires exactly one typed request")
        for value in self.evidence_paths:
            path = Path(value)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or len(value) > 300
                or any(part.startswith(".") or part in {"node_modules", "__pycache__"} for part in path.parts)
            ):
                raise ValueError("Evidence must identify bounded public workspace paths")
        return self


class NativeAllowanceWorkerFeedback(WorkerFeedback):
    """Feedback for the one native, administrator-reserved final provider attempt."""

    protocol: Literal["native-provider-recovery-allowance-v1"]
    attempt: Literal[5]
    role_id: Literal["implementer"]
    task_id: Literal["implement"]
    allowance_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    allowance_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("attempt", mode="before")
    @classmethod
    def exact_attempt(cls, value: Any) -> int:
        if type(value) is not int or value != 5:
            raise ValueError("Native final allowance feedback must identify exact attempt five")
        return value


class SourceBudgetWorkerFeedback(WorkerFeedback):
    """Feedback bound to one explicitly amended canonical source activation."""

    protocol: Literal["source-budget-continuation-v1"]
    attempt: int = Field(ge=5, le=32, strict=True)
    role_id: Literal["implementer"]
    task_id: Literal["implement"]
    allowance_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    allowance_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def worker_feedback_model(value: Any) -> type[WorkerFeedback]:
    if isinstance(value, SourceBudgetWorkerFeedback) or (
        isinstance(value, dict) and value.get("protocol") == "source-budget-continuation-v1"
    ):
        return SourceBudgetWorkerFeedback
    if isinstance(value, NativeAllowanceWorkerFeedback) or (
        isinstance(value, dict) and value.get("protocol") == "native-provider-recovery-allowance-v1"
    ):
        return NativeAllowanceWorkerFeedback
    return WorkerFeedback


class WorkerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
    actor: str = Field(min_length=1, max_length=200)
    answer: str = Field(min_length=1, max_length=8000)
