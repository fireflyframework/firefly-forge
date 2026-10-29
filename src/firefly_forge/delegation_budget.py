# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Derive shared factory-team spending from immutable native reservations."""

from __future__ import annotations

import copy
import re
from typing import Any

from firefly_forge.delegation_models import FactoryTeamV1
from firefly_forge.journal import digest

Data = dict[str, Any]
RESOURCES = ("requests", "tool_calls", "commands", "broker_calls")
LIFECYCLE = {
    "child_source_ready",
    "child_claimed",
    "child_cancel_requested",
    "child_completed",
    "child_failed",
    "child_cancelled",
    "child_needs_reconciliation",
    "join",
    "wait_observed",
    "wait_started",
    "wait_result",
    "wait_resumed",
}


def _identity(value: Any) -> bool:
    return type(value) is str and re.fullmatch(r"[a-f0-9]{32}", value) is not None


def derive_family_budget(family_id: str, frozen: FactoryTeamV1 | Data | None, entries: list[Data]) -> Data | None:
    if frozen is None:
        return None
    try:
        team = frozen if isinstance(frozen, FactoryTeamV1) else FactoryTeamV1.model_validate(frozen)
        if not _identity(family_id):
            raise ValueError("Invalid family identity")
        limits = team.budget.model_dump()
        charged, attempted, unknown = (dict.fromkeys(RESOURCES, 0) for _ in range(3))
        reservations: Data = {}
        per_tool_charged: dict[str, int] = {}
        members, keys = {family_id}, set()
        previous = "0" * 64
        for sequence, entry in enumerate(entries, 1):
            if (
                set(entry) != {"family_id", "sequence", "previous_hash", "kind", "key", "payload", "digest"}
                or entry["family_id"] != family_id
                or type(entry["sequence"]) is not int
                or entry["sequence"] != sequence
                or entry["previous_hash"] != previous
                or type(entry["key"]) is not str
                or not entry["key"]
                or entry["key"] in keys
                or type(entry["payload"]) is not dict
                or digest({key: value for key, value in entry.items() if key != "digest"}) != entry["digest"]
            ):
                raise ValueError("Invalid family event custody")
            keys.add(entry["key"])
            previous = entry["digest"]
            kind, payload = entry["kind"], entry["payload"]
            if kind == "child_admitted":
                if (
                    payload.get("parent_run_id") not in members
                    or not _identity(payload.get("child_run_id"))
                    or payload["child_run_id"] in members
                ):
                    raise ValueError("Invalid child admission lineage")
                members.add(payload["child_run_id"])
            elif kind == "budget_reserved":
                if (
                    set(payload)
                    != {"member_run_id", "resource", "units"}
                    | ({"tool"} if payload.get("resource") in {"tool_calls", "broker_calls"} else set())
                    or payload["member_run_id"] not in members
                    or payload["resource"] not in RESOURCES
                    or type(payload["units"]) is not int
                    or payload["units"] <= 0
                ):
                    raise ValueError("Invalid family resource reservation")
                resource, units = payload["resource"], payload["units"]
                if resource in {"tool_calls", "broker_calls"}:
                    tool = payload["tool"]
                    if (
                        type(tool) is not str
                        or re.fullmatch(r"[a-z][a-z0-9_]*|external_mcp:[a-z][a-z0-9-]{0,30}:[a-z][a-z0-9_]{0,80}", tool)
                        is None
                    ):
                        raise ValueError("Invalid shared tool identity")
                    if resource == "broker_calls":
                        per_tool_charged[tool] = per_tool_charged.get(tool, 0) + units
                        if (
                            team.broker_per_tool_limit is not None
                            and per_tool_charged[tool] > team.broker_per_tool_limit
                        ):
                            raise ValueError("Approved family broker per-tool budget exhausted")
                charged[resource] += units
                if limits[resource] is not None and charged[resource] > limits[resource]:
                    raise ValueError("Approved family budget exhausted")
                reservations[entry["key"]] = {**payload, "state": "reserved"}
            elif kind in {"budget_dispatched", "budget_settled", "budget_unknown"}:
                if set(payload) != {"reservation_key"} or payload["reservation_key"] not in reservations:
                    raise ValueError("Resource observation lacks its admitted reservation")
                reservation = reservations[payload["reservation_key"]]
                state = reservation["state"]
                if kind == "budget_dispatched":
                    if state != "reserved":
                        raise ValueError("A resource reservation cannot dispatch twice")
                    attempted[reservation["resource"]] += reservation["units"]
                    reservation["state"] = "dispatched"
                elif kind == "budget_settled":
                    if state != "dispatched":
                        raise ValueError("A known result requires its admitted dispatch")
                    reservation["state"] = "settled"
                else:
                    if state not in {"reserved", "dispatched"}:
                        raise ValueError("A terminal resource observation is immutable")
                    unknown[reservation["resource"]] += reservation["units"]
                    reservation["state"] = "unknown"
            elif kind not in LIFECYCLE:
                raise ValueError("Unknown family lifecycle or resource operation")
    except (KeyError, TypeError, ValueError):
        raise ValueError("Factory family budget history does not verify") from None
    return {
        "version": 1,
        "family_id": family_id,
        "approved": limits,
        "charged": charged,
        "attempted": attempted,
        "unknown": unknown,
        "remaining": {name: None if limits[name] is None else limits[name] - charged[name] for name in RESOURCES},
        "members": sorted(members),
        "reservations": copy.deepcopy(reservations),
        "per_tool_charged": per_tool_charged,
        "journal_head": previous,
    }
