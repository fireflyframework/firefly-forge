# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Frozen team inheritance and deterministic source-proposal joins."""

from __future__ import annotations

import copy
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from firefly_forge.delegation_models import DelegationStartInput, FactoryTeamV1, FamilyBudget
from firefly_forge.journal import digest
from firefly_forge.models import RunInput
from firefly_forge.store import now

Data = dict[str, Any]
FROZEN_FIELDS = (
    "execution_actor",
    "component_snapshot",
    "scm_snapshot",
    "policy_snapshot",
    "role_snapshot",
    "role_selection_source",
    "model_selection",
    "review_model_selection",
    "config_revision",
    "execution_config",
    "review_config_revision",
    "review_execution_config",
    "knowledge_snapshot",
    "learning_snapshot",
    "mcp_snapshot",
    "tool_policy_snapshot",
    "sandbox_snapshot",
    "search_snapshot",
    "runner_config_revision",
    "work_duration",
    "framework_knowledge_request",
    "framework_knowledge_pin",
    "knowledge_context",
    "knowledge_context_digest",
    "knowledge_context_pin_digest",
    "branding_snapshot",
    "design_snapshot",
    "factory_team",
)


def team_capture(run: Data) -> Data | None:
    if run.get("work_duration") is None:
        return None
    limits = run["tool_policy_snapshot"]["settings"]
    configuration = run["sandbox_snapshot"]["configuration"]
    commands = configuration.get("command_limit", configuration.get("profile", {}).get("command_limit"))
    selected = (run.get("component_snapshot") or {}).get("project_factory_team")
    if selected is None:
        return FactoryTeamV1(
            budget=FamilyBudget(requests=100, tool_calls=300, commands=commands, broker_calls=limits["calls_limit"]),
            broker_per_tool_limit=limits["per_tool_limit"],
        ).model_dump(mode="json")
    return FactoryTeamV1.model_validate(selected).model_dump(mode="json")


def child_run(parent: Data, request: DelegationStartInput, source: Data) -> Data:
    team = FactoryTeamV1.model_validate(parent.get("factory_team"))
    if parent["agent"] not in {"api", "agentic", "bridge"}:
        raise ValueError("Factory delegation requires the configured framework agent path")
    child_id = uuid5(NAMESPACE_URL, f"forge:child:{parent['id']}:{request.request_id}").hex
    context = {
        "version": 1,
        "family_id": parent.get("factory_context", {}).get("family_id", parent["id"]),
        "parent_run_id": parent["id"],
        "parent_attempt": parent["attempt"],
        "request": request.model_dump(mode="json"),
        "source": copy.deepcopy(source),
        "parent_approval_digest": digest({name: parent.get(name) for name in FROZEN_FIELDS}),
    }
    if parent.get("framework_knowledge_pin") is not None:
        context["parent_framework_pin"] = copy.deepcopy(parent["framework_knowledge_pin"])
        context["parent_knowledge_context_pin_digest"] = parent.get("knowledge_context_pin_digest")
    context["digest"] = digest(context)
    values = {
        **{name: copy.deepcopy(parent[name]) for name in RunInput.model_fields if name in parent},
        **{name: copy.deepcopy(parent[name]) for name in FROZEN_FIELDS if name in parent},
        "id": child_id,
        "repository_name": parent["repository_name"],
        "brief": request.brief,
        "operation": "build",
        "publish": False,
        "assurance": {"review_agent": None, "review_model_profile_id": None},
        "factory_team": team.model_dump(mode="json"),
        "factory_context": context,
        "status": "queued",
        "created_at": now(),
        "updated_at": now(),
        "repair_budget": 0,
        "verification": [],
        "changed_files": [],
        "diff": "",
        "artifacts": [],
        "error": None,
        "summary": "",
        "branch": "",
        "base_revision": "",
    }
    values.pop("source_checkpoint", None)
    values.pop("framework_knowledge_pin", None)
    values.pop("knowledge_context_pin_digest", None)
    return values


def deterministic_join(results: list[Data], current_source: dict[str, str]) -> Data:
    ordered = sorted(copy.deepcopy(results), key=lambda item: (item["task_id"], item["delegation_id"]))
    holds: list[str] = []
    seen: set[str] = set()
    paths: set[str] = set()
    changes: list[Data] = []
    for child in ordered:
        identity = child["delegation_id"]
        if identity in seen:
            holds.append("duplicate_child")
        seen.add(identity)
        if child["state"] != "completed" or not child.get("receipt_digest"):
            holds.append("child_not_completed")
        for change in child["changes"]:
            path = change["path"]
            if path in paths:
                holds.append("shared_file_conflict")
            paths.add(path)
            if current_source.get(path) != change["base_sha256"]:
                holds.append("parent_source_changed")
            changes.append(copy.deepcopy(change))
    return {
        "version": 1,
        "status": "held" if holds else "ready",
        "holds": sorted(set(holds)),
        "children": [
            {key: item[key] for key in ("delegation_id", "task_id", "state", "summary", "receipt_digest")}
            for item in ordered
        ],
        "changes": [] if holds else sorted(changes, key=lambda item: item["path"]),
        "applied": False,
        "delivery_verified": False,
    }
