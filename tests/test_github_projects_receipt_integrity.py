# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Unknown request disposition cannot diverge from immutable native receipts."""

import copy

import pytest

from firefly_forge.execution_requests import digest
from firefly_forge.github_projects_requests import checked_board_request


def retained():
    incoming = {"input": {"request_id": "1" * 32}, "actor": {"user_id": "2" * 32}, "run_id": "3" * 32}
    data = {
        "id": "1" * 32,
        "kind": "github_board",
        "run_id": "3" * 32,
        "status": "unknown",
        "request": incoming,
        "request_digest": digest(incoming),
        "result": {"reason": "worker_restarted_no_replay"},
    }
    events = []
    for kind, evidence in [
        ("request_admitted", {"request_digest": data["request_digest"]}),
        ("worker_claim", {"request_digest": data["request_digest"]}),
        ("worker_unknown", data["result"]),
    ]:
        entry = {
            "request_id": data["id"],
            "sequence": len(events) + 1,
            "previous_hash": events[-1]["digest"] if events else "0" * 64,
            "kind": kind,
            "evidence": copy.deepcopy(evidence),
            "created_at": "2026-10-24T00:00:00Z",
        }
        entry["digest"] = digest(entry)
        events.append(entry)
    return data, events


def append(events, kind, evidence):
    row = {
        "request_id": "1" * 32,
        "sequence": len(events) + 1,
        "previous_hash": events[-1]["digest"],
        "kind": kind,
        "evidence": evidence,
        "created_at": "2026-10-24T00:00:01Z",
    }
    row["digest"] = digest(row)
    events.append(row)


def test_unknown_remains_held_after_observation():
    data, events = retained()
    append(events, "read_only_observation", {"replay_authorized": False})
    checked_board_request(data, events)
    changed = copy.deepcopy(data)
    changed["status"] = "completed"
    with pytest.raises(ValueError):
        checked_board_request(changed, events)


@pytest.mark.parametrize("change", ["status", "result", "actor", "digest", "request_id", "extra_claim"])
def test_row_or_outcome_cannot_diverge_from_immutable_events(change):
    data, events = retained()
    if change == "status":
        data["status"] = "completed"
    elif change == "result":
        data["result"] = {"reason": "completed"}
    elif change == "actor":
        data["request"]["actor"]["user_id"] = "4" * 32
    elif change == "digest":
        data["request_digest"] = "5" * 64
    elif change == "request_id":
        data["id"] = "6" * 32
    else:
        append(events, "worker_claim", {"request_digest": data["request_digest"]})
    with pytest.raises(ValueError):
        checked_board_request(data, events)
