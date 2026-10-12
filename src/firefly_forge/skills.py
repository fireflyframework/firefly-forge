# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Versioned instruction skills and the actual factory execution roles."""

from __future__ import annotations

from typing import Any

SKILLS = [
    {
        "id": "implementation",
        "name": "Framework implementation",
        "description": "Build maintainable source using the selected Firefly framework.",
        "default": True,
        "instructions": (
            "Inspect existing code and repository instructions first. Implement the brief u"
            "sing real framework imports, dependency injection and service boundaries. Keep"
            " the change bounded and avoid unrelated refactors."
        ),
    },
    {
        "id": "verification",
        "name": "Meaningful verification",
        "description": "Exercise product behavior, errors and executable checks.",
        "default": True,
        "instructions": (
            "Add meaningful behavioral tests before fixing behavior. Cover valid input, val"
            "idation, conflicts and regression cases. Provide executable checks for tests, "
            "lint, types and build where configured. Never disable checks, fake passing out"
            "put or use no-op tests."
        ),
    },
    {
        "id": "security",
        "name": "Secure application boundaries",
        "description": "Preserve authentication, validation and credential safety.",
        "default": True,
        "instructions": (
            "Preserve authentication, CSRF and authorization boundaries. Validate input and"
            " constrain file access. Never read, generate or print private credentials. Use"
            " environment examples with placeholders and preserve safe error handling."
        ),
    },
    {
        "id": "accessibility",
        "name": "Accessible product experience",
        "description": "Build usable forms, navigation and status feedback.",
        "default": False,
        "instructions": (
            "Use semantic controls with labels, visible keyboard focus, accessible errors a"
            "nd status announcements. Check narrow screens, keyboard navigation and loading"
            "/error states. Test actual user actions rather than component existence."
        ),
    },
    {
        "id": "documentation",
        "name": "Product handoff",
        "description": "Explain installation, use, checks and operational limits.",
        "default": False,
        "instructions": (
            "Write a concise product README with reproducible installation, configuration, "
            "usage and verification steps. Describe actual behavior and limitations. Keep s"
            "ecrets and absolute local machine paths out of source."
        ),
    },
]
DEFAULT_SKILLS = [str(item["id"]) for item in SKILLS if item["default"]]


def skill_catalog() -> dict[str, Any]:
    return {"skills": [dict(item) for item in SKILLS]}


def validate_skills(skills: list[str]) -> list[str]:
    known = {item["id"] for item in SKILLS}
    if set(skills) - known:
        raise ValueError("Unknown skill selection")
    if len(set(skills)) != len(skills):
        raise ValueError("Select each skill only once")
    return skills


def inject_skills(brief: str, selected: list[str]) -> str:
    validate_skills(selected)
    if not selected:
        return brief
    instructions = [f"[{item['id']}] {item['instructions']}" for item in SKILLS if item["id"] in selected]
    return brief + "\n\nSelected reusable skills:\n" + "\n\n".join(instructions)


def workflow_catalog() -> dict[str, Any]:
    stages = [
        ("queued", "Queue", "Freeze the chosen provider settings and skills.", ["orchestrator"], []),
        (
            "preparing",
            "Prepare workspace",
            "Clone the selected branch into an isolated run workspace.",
            ["orchestrator"],
            ["git"],
        ),
        (
            "developing",
            "Implement",
            "A single selected coding provider edits actual source and tests.",
            ["builder"],
            ["workspace", "provider"],
        ),
        (
            "validating",
            "Verify",
            "The factory executes real repository checks and records their outcomes.",
            ["verifier"],
            ["commands", "git"],
        ),
        (
            "repairing",
            "Repair",
            "The same coding provider receives failing checks within a bounded repair budget.",
            ["builder", "verifier"],
            ["workspace", "provider", "commands"],
        ),
        (
            "succeeded",
            "Review ready",
            "Retain the passing source snapshot, diff and changed-file archive.",
            ["orchestrator"],
            ["git", "archive"],
        ),
        (
            "publication",
            "Publish for review",
            "Explicitly requested publication pushes a verified branch and creates a pull request.",
            ["publisher"],
            ["git", "scm"],
        ),
    ]
    roles = [
        (
            "orchestrator",
            "Factory orchestrator",
            "Durable queue, isolated workspace, cancellation and evidence lifecycle.",
            True,
        ),
        (
            "builder",
            "Coding worker",
            (
                "The selected real provider implements and repairs the brief; this is one worke"
                "r, not simulated specialist agents."
            ),
            False,
        ),
        (
            "verifier",
            "Verification runner",
            "Deterministic commands independently test the generated source; this role is not a model.",
            True,
        ),
        (
            "publisher",
            "Source control publisher",
            "Publish verified source for external human review; never merge automatically.",
            True,
        ),
    ]
    tools = [
        (
            "workspace",
            "Workspace source tools",
            (
                "Native Agentic exposes confined list_files, read_file and write_file; CLI work"
                "ers use their official tools."
            ),
        ),
        ("provider", "Coding provider", "Official Codex/Claude Code CLI or real native Firefly Agentic execution."),
        (
            "commands",
            "Verification commands",
            "Bounded server subprocesses for supplied commands or detected executable checks.",
        ),
        ("git", "Git workspace", "Isolated clone, source snapshots and explicit publication."),
        ("archive", "Changed-source export", "Safe changed files and patch, not dependency or credential directories."),
        ("scm", "Source control API", "Provider-specific review request creation after passing gates."),
    ]
    return {
        "stages": [{"id": i, "name": n, "description": d, "roles": r, "tools": t} for i, n, d, r, t in stages],
        "agents": [
            {"id": i, "name": n, "role": i, "description": d, "provider_independent": independent}
            for i, n, d, independent in roles
        ],
        "tools": [{"id": i, "name": n, "description": d} for i, n, d in tools],
    }
