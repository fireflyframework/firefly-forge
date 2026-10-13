# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Read projections of implemented participants, separate from immutable run role bindings."""

from __future__ import annotations

import copy
from typing import Any

from firefly_forge.agent_roles import ProjectRoles, RoleRegistry, role_digest

Data = dict[str, Any]
ASSISTANT_SKILLS = {
    "product": ["thinking", "evidence-first"],
    "functions": ["thinking", "evidence-first"],
    "design": ["thinking", "frontend-design", "accessibility"],
    "architecture": ["thinking", "evidence-first"],
}


def assistant_instruction_snapshot(registry: RoleRegistry, task: str, policy: Data) -> Data:
    selected = ASSISTANT_SKILLS[task]
    catalog = {skill["id"]: skill for skill in registry._skills(policy)}
    skills = [copy.deepcopy(catalog[ident]) for ident in selected]
    payload = {"task": task, "policy_revision": policy["revision"], "skills": skills}
    return {**payload, "digest": role_digest(payload)}


def inject_assistant_instructions(prompt: str, snapshot: Data) -> str:
    payload = {key: value for key, value in snapshot.items() if key != "digest"}
    if snapshot.get("digest") != role_digest(payload):
        raise ValueError("Assistant instruction snapshot digest mismatch")
    instructions = "\n\n".join(f"[{skill['id']}] {skill['instructions']}" for skill in snapshot["skills"])
    return (
        "Frozen dialogue instruction skills:\n"
        + instructions
        + "\n\nApply these skills within this tool-free dialogue. Produce proposals for owner review; "
        "do not execute commands or infer approval.\n\n" + prompt
    )


PHASES = [
    (
        "product_definition",
        "Define the product",
        "Capture customer outcomes, journeys and functional behavior.",
        "Reviewed product and feature decisions",
        ["product", "functions"],
    ),
    (
        "design",
        "Design the experience",
        "Propose product design, then review actual references and wireframes.",
        "Saved design references and owner review",
        ["design"],
    ),
    (
        "architecture",
        "Review architecture",
        "Propose component boundaries and retain reviewed contracts.",
        "Reviewed architecture and component contracts",
        ["architecture"],
    ),
    (
        "prepare",
        "Prepare",
        "Freeze approved contracts and isolate the selected source.",
        "Frozen contract, source revision and execution snapshots",
        [],
    ),
    (
        "implement",
        "Implement",
        "One selected coding provider edits source and tests, including bounded repairs.",
        "Changed source, tests and typed worker feedback",
        [],
    ),
    (
        "verify",
        "Verify",
        "Run repository checks independently of worker claims.",
        "Executable check outcomes against the actual source tree",
        [],
    ),
    (
        "checkpoint",
        "Checkpoint",
        "Retain a verified source snapshot and bounded delivery evidence.",
        "Source tree, diff, archives and journal checkpoint",
        [],
    ),
    (
        "assurance",
        "Review assurance",
        "Optionally request an independent provider review of the verified tree.",
        "Retained assurance findings bound to the source tree",
        [],
    ),
    (
        "publication",
        "Publish for review",
        "Explicitly publish verified source through source control.",
        "Publication intent and provider review-request receipt",
        [],
    ),
    (
        "deploy",
        "Deploy",
        "Explicitly request delivery to a configured target.",
        "Deployment intent, execution receipts and reconciliation status",
        [],
    ),
    (
        "observe",
        "Monitor",
        "Probe configured applications and retain observations and incidents.",
        "Health observations, incident records and thresholds",
        [],
    ),
    (
        "maintain",
        "Maintain and support",
        "Explicitly request a maintenance run using incident and product evidence.",
        "A maintain run through the same implementation and verification gates",
        [],
    ),
]


def _skills(entries: list[Data], application: str) -> list[Data]:
    return [
        {
            key: copy.deepcopy(value)
            for key, value in item.items()
            if key
            in {"id", "name", "description", "instructions", "provenance", "adaptation", "adaptation_sha256", "kind"}
        }
        | {"application": application}
        for item in entries
    ]


def lifecycle_catalog(
    registry: RoleRegistry,
    *,
    overrides: ProjectRoles | None = None,
    policy: Data | None = None,
    knowledge: Data | None = None,
    routing: Data | None = None,
    profiles: list[Data] | None = None,
    repository_id: str | None = None,
    assistant_policy: Data | None = None,
    tools: list[Data] | None = None,
) -> Data:
    if policy is None:
        from firefly_forge.policies import PolicyCatalog

        policy = PolicyCatalog(registry.private.directory.parent).current()
    current = registry.current()
    effective = registry.snapshot(overrides, policy, tool_catalog=tools)
    if current["revision"] != effective["revision"]:
        raise ValueError("Role configuration changed while reading the lifecycle; refresh the catalog")
    known = {item["id"]: item for item in registry._skills(policy)}
    profiles_by_id = {item["id"]: item for item in profiles or []}
    knowledge = knowledge or {}
    configured = bool(knowledge.get("enabled"))
    knowledge_summary = {
        "status": "configured_unverified" if configured else "not_configured",
        "project_scope": "selected_repository" if repository_id else "selected_at_invocation",
        "framework_scope": "configured" if knowledge.get("framework_workspace") else "not_configured",
        "required": bool(knowledge.get("required")),
        "verification": (
            "Invocation citations and receipts establish retrieval; configuration does not establish access."
        ),
    }

    def selection(task: str) -> Data:
        ident = (routing or {}).get("bindings", {}).get(task)
        profile = profiles_by_id.get(ident)
        return {
            "status": "configured_unverified" if profile else "invocation_selected",
            "profile_id": ident if profile else None,
            "provider": profile["provider"] if profile else None,
            "task": task,
        }

    phases = []
    for ident, name, description, handoff, tasks in PHASES:
        participants = []
        for task in tasks:
            instructions = assistant_instruction_snapshot(registry, task, assistant_policy or policy)
            participants.append(
                {
                    "id": "assistant-" + task,
                    "name": task.title() + " assistant",
                    "task": task,
                    "execution_kind": "configured_model",
                    "enabled": True,
                    "optional": True,
                    "responsibilities": ["Propose and clarify; product-owner review remains separate"],
                    "inputs": ["Product context, owner request and approved source observations"],
                    "outputs": ["Dialogue receipt, questions and proposed product decisions"],
                    "skills": _skills(instructions["skills"], "injected_instruction"),
                    "tools": [],
                    "knowledge_scope": "project_and_configured_framework",
                    "selection": selection(task),
                }
            )
        for role in effective["roles"]:
            if ident not in role["stages"]:
                continue
            worker = role["execution_mode"] == "coding_provider"
            participants.append(
                {
                    **{
                        key: copy.deepcopy(role[key])
                        for key in ["id", "name", "enabled", "responsibilities", "inputs", "outputs", "instructions"]
                    },
                    "role_id": role["id"],
                    "execution_kind": "run_provider" if worker else "deterministic",
                    "optional": role["id"] == "docs",
                    "skills": _skills(
                        [known[skill] for skill in role["skills"]],
                        "injected_instruction" if worker else "instruction_metadata",
                    ),
                    "tools": [
                        copy.deepcopy(tool) for tool in effective["tool_catalog"] if tool["id"] in role["allowed_tools"]
                    ],
                    "knowledge_scope": "project_and_configured_framework" if worker else "frozen_evidence",
                    "selection": selection("implementation") if worker else {"status": "factory_gate"},
                    "policy_skills": _skills(policy["skills"], "injected_instruction") if worker else [],
                }
            )
        physical = {
            "assurance": ("assurance-reviewer", "Independent assurance reviewer", "run_provider", "review", True),
            "publication": ("scm-publisher", "Source control publisher", "deterministic", None, False),
            "deploy": ("deployment-manager", "Deployment manager", "deterministic", None, False),
            "observe": ("application-observer", "Application observer", "deterministic", None, False),
            "maintain": ("maintenance-worker", "Maintenance worker", "run_provider", "implementation", True),
        }.get(ident)
        if physical:
            pid, pname, kind, physical_task, optional = physical
            maintenance = ident == "maintain"
            worker_roles = [
                role for role in effective["roles"] if role["enabled"] and role["execution_mode"] == "coding_provider"
            ]
            maintenance_skills = sorted({skill for role in worker_roles for skill in role["skills"]})
            participants.append(
                {
                    "id": pid,
                    "name": pname,
                    "execution_kind": kind,
                    "enabled": True,
                    "optional": optional,
                    "responsibilities": [description],
                    "inputs": [
                        "Explicit authorized request and retained evidence"
                        if ident != "observe"
                        else "Configured application health and metric targets"
                    ],
                    "outputs": [handoff],
                    "skills": _skills([known[skill] for skill in maintenance_skills], "injected_instruction")
                    if maintenance
                    else [],
                    "policy_skills": _skills(policy["skills"], "injected_instruction") if maintenance else [],
                    "tools": [
                        tool
                        for tool in effective["tool_catalog"]
                        if any(tool["id"] in role["allowed_tools"] for role in worker_roles)
                    ]
                    if maintenance
                    else [],
                    "knowledge_scope": "project_and_configured_framework" if maintenance else "frozen_evidence",
                    "selection": selection(physical_task)
                    if physical_task
                    else {"status": "configured_target" if ident in {"deploy", "observe"} else "explicit_request"},
                    "condition": "Explicit review_agent or review task profile on the run"
                    if ident == "assurance"
                    else "Explicit operation=maintain; uses the same frozen coding guidance"
                    if ident == "maintain"
                    else "Configured target and authorized action",
                }
            )
        phases.append(
            {"id": ident, "name": name, "description": description, "handoff": handoff, "participants": participants}
        )
    return {
        "version": "1",
        "scope": {"repository_id": repository_id},
        "registry_revision": current["revision"],
        "registry_digest": current["digest"],
        "effective_digest": effective["digest"],
        "document": current["document"],
        "effective_roles": effective["roles"],
        "overrides": (overrides or ProjectRoles()).model_dump(),
        "knowledge": knowledge_summary,
        "phases": phases,
        "tools": effective["tool_catalog"],
        "skills": _skills(list(known.values()), "catalog"),
        "policies": [
            *_skills([s for s in policy["skills"] if s["kind"] != "quality_gate"], "injected_instruction"),
            *_skills([s for s in policy["skills"] if s["kind"] == "quality_gate"], "executable_gate"),
        ],
        "limits": (
            "Current configuration, not a live execution. "
            "Existing runs and retained turns keep their frozen snapshots. "
            "External data requires configured grants; CLI tool authority is separate."
        ),
    }
