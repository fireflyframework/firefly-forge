# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native plan history is observed separately from the original immutable plan."""

from __future__ import annotations

import copy
import hashlib
import re
from typing import TYPE_CHECKING, Any

from firefly_forge.journal import Journal, digest

if TYPE_CHECKING:
    from firefly_forge.run_evidence import EvidenceSnapshot

Data = dict[str, Any]
_PLAN_FIELDS = (
    "version",
    "revision",
    "run_id",
    "repository_id",
    "project_id",
    "component_id",
    "operation",
    "brief",
    "backend",
    "frontend",
    "tasks",
    "commands",
)
_SCOPE_FIELDS = ("repository_id", "project_id", "component_id", "component_snapshot", "role_snapshot")
_TRANSITION_FIELDS = {
    "mode",
    "previous_agent",
    "agent",
    "previous_config_revision",
    "config_revision",
    "previous_model_selection",
    "model_selection",
}


def require(value: Any) -> None:
    if not value:
        from firefly_forge.run_evidence import EvidenceUnavailable

        raise EvidenceUnavailable("evidence_integrity")


def native_id(value: Any) -> bool:
    return type(value) is str and re.fullmatch(r"[a-f0-9]{32}", value) is not None


def candidates(plan: Data, previous: Data | None, journal: list[Data]) -> list[Data]:
    anchors = [
        entry
        for entry in journal
        if entry["kind"] == "plan"
        and entry["task_id"] == "plan"
        and entry["payload"] == {"revision": plan["revision"], "digest": plan["digest"]}
    ]
    if anchors:
        require(len(anchors) == 1)
        return [
            {
                "sequence": anchors[0]["sequence"],
                "digest": anchors[0]["digest"],
                "provenance": "immutable_wal_plan",
                "full_plan_digest_in_wal": True,
            }
        ]
    require(previous is not None)
    old = (previous or {})["data"]
    data = plan["data"]
    transition = data.get("selection_transition")
    require(type(transition) is dict and set(transition) == _TRANSITION_FIELDS)
    require(transition["mode"] in {"frozen", "named_profile", "legacy_provider"})
    require(
        transition["previous_agent"] == old.get("agent")
        and transition["previous_config_revision"] == old.get("config_revision")
        and transition["previous_model_selection"] == old.get("model_selection")
    )
    expected = {
        **old,
        "revision": plan["revision"],
        "agent": transition["agent"],
        "config_revision": transition["config_revision"],
        "execution_config": data.get("execution_config"),
        "model_selection": transition["model_selection"],
        "review_model_selection": data.get("review_model_selection"),
        "selection_transition": transition,
    }
    require(data == expected)
    fields = {
        **{key: value for key, value in transition.items() if key != "mode"},
        "selection_mode": transition["mode"],
    }
    matches = [
        entry
        for entry in journal
        if entry["kind"] in {"handoff", "resume"}
        and entry["task_id"] == entry["kind"]
        and all(key in entry["payload"] and entry["payload"][key] == value for key, value in fields.items())
    ]
    require(matches)
    return [
        {
            "sequence": entry["sequence"],
            "digest": entry["digest"],
            "provenance": "observed_native_plan_with_selection_handoff",
            "full_plan_digest_in_wal": False,
        }
        for entry in matches
    ]


def ordered_evidence(plans: list[Data], journal: list[Data]) -> list[Data]:
    states: list[tuple[int, int, list[Data]]] = [(-1, 1, [])]
    for index, plan in enumerate(plans):
        matches = sorted(
            candidates(plan, plans[index - 1] if index else None, journal), key=lambda row: row["sequence"]
        )
        advanced: list[tuple[int, int, list[Data]]] = []
        position = count = 0
        first: list[Data] | None = None
        for match in matches:
            while position < len(states) and states[position][0] < match["sequence"]:
                _, ways, path = states[position]
                count = min(2, count + ways)
                if first is None:
                    first = path
                position += 1
            if count:
                advanced.append((match["sequence"], count, [*(first or []), match]))
        require(advanced)
        states = advanced
    require(sum(count for _, count, _ in states) == 1)
    return next(path for _, count, path in states if count == 1)


def snapshot_with_plans(
    *,
    run: Data,
    plans: list[Data],
    tasks: list[Data],
    journal: list[Data],
    tools: list[Data],
    activity: list[Data],
    project_revision: Data | None = None,
) -> EvidenceSnapshot:
    from firefly_forge.run_evidence import (
        LIMITS,
        MAX_BYTES,
        EvidenceSnapshot,
        EvidenceUnavailable,
        build_evidence,
        encoded,
    )

    require(plans and len(plans) <= LIMITS["journal"] and Journal.verify_chain(journal))
    require(all(entry["run_id"] == run["id"] for entry in journal))
    plans = sorted(plans, key=lambda row: row["revision"])
    require(len({row["id"] for row in plans}) == len(plans))
    require([row["revision"] for row in plans] == list(range(1, len(plans) + 1)))
    for row in plans:
        require(
            native_id(row["id"])
            and row["run_id"] == run["id"]
            and type(row["digest"]) is str
            and re.fullmatch(r"[a-f0-9]{64}", row["digest"]) is not None
        )
        require(type(row["revision"]) is int and row["revision"] > 0 and type(row["data"]) is dict)
        require(
            type(row["data"].get("revision")) is int
            and row["data"].get("revision") == row["revision"]
            and row["data"].get("run_id") == run["id"]
            and digest(row["data"]) == row["digest"]
        )
        require(all(row["data"].get(key) == run.get(key) for key in _SCOPE_FIELDS))
    by_id = {row["id"]: row for row in plans}
    require(len({row["id"] for row in tasks}) == len(tasks) and len({row["task_id"] for row in tasks}) == len(tasks))
    for row in tasks:
        require(native_id(row["id"]) and row["run_id"] == run["id"] and row["plan_id"] in by_id)
    lineage = ordered_evidence(plans, journal)
    root = plans[0]
    snapshot = build_evidence(
        run=run,
        plan={**root["data"], "digest": root["digest"]},
        journal=journal,
        tasks=[
            {"id": row["task_id"], "attempt": row["attempt"], "observed_status": row["status"], "data": row["data"]}
            for row in tasks
        ],
        tools=tools,
        activity=activity,
        project_revision=project_revision,
    )
    if len(plans) == 1:
        return snapshot
    bindings = [
        {
            "native_task_id": row["id"],
            "task_id": row["task_id"],
            "plan_id": row["plan_id"],
            "plan_digest": by_id[row["plan_id"]]["digest"],
        }
        for row in tasks
    ]
    history = {
        "version": 1,
        "projection_basis": "initial_immutable_plan",
        "plans": [
            {
                "native_id": row["id"],
                "revision": row["revision"],
                "source_digest": row["digest"],
                "projection": {key: copy.deepcopy(row["data"][key]) for key in _PLAN_FIELDS if key in row["data"]},
                **evidence,
            }
            for row, evidence in zip(plans, lineage, strict=True)
        ],
        "task_plan_bindings": bindings,
    }
    members = copy.deepcopy(snapshot.members)
    members["plans.json"] = history
    manifest = copy.deepcopy(snapshot.manifest)
    manifest["plan_history"] = {
        "member": "plans.json",
        "projection_basis": "initial_immutable_plan",
        "count": len(plans),
        "latest_observed": {
            "revision": plans[-1]["revision"],
            "digest": plans[-1]["digest"],
            "provenance": lineage[-1]["provenance"],
        },
        "task_plan_bindings": bindings,
        "execution_authority": False,
    }
    manifest["limits"]["plans"] = LIMITS["journal"]
    manifest["members"] = [
        {"name": name, "bytes": len(encoded(value)), "sha256": hashlib.sha256(encoded(value)).hexdigest()}
        for name, value in sorted(members.items())
    ]
    manifest["digest"] = digest({key: value for key, value in manifest.items() if key != "digest"})
    if len(encoded(manifest)) + sum(len(encoded(value)) for value in members.values()) > MAX_BYTES:
        raise EvidenceUnavailable("evidence_limit")
    return EvidenceSnapshot(manifest, copy.deepcopy(snapshot.records), members, snapshot.repositories)
