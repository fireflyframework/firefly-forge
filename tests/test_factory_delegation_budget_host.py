# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Family reservations preserve parent/child spending across failure and restart."""

import copy

import pytest

from firefly_forge.delegation_budget import derive_family_budget
from firefly_forge.delegation_models import FactoryTeamV1, FamilyBudget
from firefly_forge.journal import digest

ROOT, CHILD, OTHER = "a" * 32, "b" * 32, "c" * 32
TEAM = FactoryTeamV1(budget=FamilyBudget(requests=3, tool_calls=4, commands=2))


def append(entries, kind, key, payload):
    entry = {
        "family_id": ROOT,
        "sequence": len(entries) + 1,
        "previous_hash": entries[-1]["digest"] if entries else "0" * 64,
        "kind": kind,
        "key": key,
        "payload": payload,
    }
    entry["digest"] = digest(entry)
    entries.append(entry)


def child(entries):
    append(entries, "child_admitted", "child", {"parent_run_id": ROOT, "child_run_id": CHILD})


def reserve(entries, key, member=ROOT, resource="requests", units=1):
    append(entries, "budget_reserved", key, {"member_run_id": member, "resource": resource, "units": units})


def test_absent_legacy_team_does_not_acquire_or_reset_an_envelope():
    assert derive_family_budget(ROOT, None, []) is None


def test_parent_and_child_share_one_envelope_and_unknown_work_stays_charged():
    entries = []
    child(entries)
    reserve(entries, "parent-request")
    reserve(entries, "child-request", CHILD)
    append(entries, "budget_dispatched", "child-dispatch", {"reservation_key": "child-request"})
    append(entries, "budget_unknown", "child-unknown", {"reservation_key": "child-request"})
    before = copy.deepcopy(entries)
    state = derive_family_budget(ROOT, TEAM, entries)
    assert state["charged"] == {"requests": 2, "tool_calls": 0, "commands": 0}
    assert state["remaining"] == {"requests": 1, "tool_calls": 4, "commands": 2}
    assert state["attempted"]["requests"] == 1
    assert state["unknown"]["requests"] == 1
    assert derive_family_budget(ROOT, TEAM.model_dump(mode="json"), copy.deepcopy(entries)) == state
    assert entries == before


@pytest.mark.parametrize("resource,limit", [("requests", 3), ("tool_calls", 4), ("commands", 2)])
def test_no_member_can_multiply_the_approved_aggregate_budget(resource, limit):
    entries = []
    child(entries)
    reserve(entries, "parent", resource=resource, units=limit)
    reserve(entries, "child-charge", CHILD, resource=resource)
    with pytest.raises(ValueError, match="budget"):
        derive_family_budget(ROOT, TEAM, entries)


@pytest.mark.parametrize("case", ["duplicate", "foreign_member", "cycle", "foreign_family", "boolean_units", "reset"])
def test_rehashed_invalid_history_cannot_authorize_or_restore_spending(case):
    entries = []
    child(entries)
    reserve(entries, "first")
    if case == "duplicate":
        reserve(entries, "first")
    elif case == "foreign_member":
        reserve(entries, "other", OTHER)
    elif case == "cycle":
        append(entries, "child_admitted", "cycle", {"parent_run_id": CHILD, "child_run_id": ROOT})
    elif case == "foreign_family":
        entries[-1]["family_id"] = OTHER
        entries[-1]["digest"] = digest({k: v for k, v in entries[-1].items() if k != "digest"})
    elif case == "boolean_units":
        reserve(entries, "boolean", units=True)
    elif case == "reset":
        append(entries, "budget_reset", "reset", {"requests": 0})
    with pytest.raises(ValueError):
        derive_family_budget(ROOT, TEAM, entries)


@pytest.mark.parametrize("terminal", ["budget_settled", "budget_unknown"])
def test_terminal_observation_does_not_refund_or_allow_redispatch(terminal):
    entries = []
    reserve(entries, "first")
    append(entries, "budget_dispatched", "dispatch", {"reservation_key": "first"})
    append(entries, terminal, "observation", {"reservation_key": "first"})
    state = derive_family_budget(ROOT, TEAM, entries)
    assert state["remaining"]["requests"] == 2
    append(entries, "budget_dispatched", "redispatch", {"reservation_key": "first"})
    with pytest.raises(ValueError):
        derive_family_budget(ROOT, TEAM, entries)
