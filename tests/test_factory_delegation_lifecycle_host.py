# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Child lifecycle observations derive only from their admitted native family events."""

import copy

import pytest

from firefly_forge.delegation_models import DelegationStartInput
from firefly_forge.journal import digest

ROOT, CHILD = "a" * 32, "b" * 32


def event(kind, payload, sequence):
    value = {"kind": kind, "payload": payload, "sequence": sequence}
    return {**value, "digest": digest(value)}


def admitted():
    return event(
        "child_admitted",
        {
            "parent_run_id": ROOT,
            "child_run_id": CHILD,
            "parent_task_key": "c" * 32,
            "request": DelegationStartInput(request_id="d" * 32, task_id="review", brief="Review source").model_dump(),
            "approval_digest": "e" * 64,
            "source_digest": "f" * 64,
        },
        1,
    )


def claim(admission):
    return event(
        "child_claimed",
        {
            "child_run_id": CHILD,
            "admission_digest": admission["digest"],
            "generation": "1" * 32,
            "owner_pid": 123,
            "source_digest": "f" * 64,
        },
        2,
    )


def result(claimed, state="completed"):
    return event(
        "child_" + state,
        {
            "child_run_id": CHILD,
            "claim_digest": claimed["digest"],
            "result": {"summary": "Observed source", "changes": [], "delivery_verified": False},
        },
        3,
    )


def derive(events):
    from firefly_forge.delegation_lifecycle import child_lifecycle

    return child_lifecycle(events)


def test_new_instance_derives_queued_running_and_terminal_from_original_claim():
    admission = admitted()
    claimed = claim(admission)
    assert derive([admission])[CHILD]["state"] == "queued"
    assert derive([admission, claimed])[CHILD]["state"] == "running"
    terminal = result(claimed)
    current = derive([admission, claimed, terminal])[CHILD]
    assert current["state"] == "completed" and current["receipt_digest"] == terminal["digest"]
    assert current["claim_digest"] == claimed["digest"] and current["delivery_verified"] is False


@pytest.mark.parametrize("case", ["duplicate_claim", "wrong_admission", "wrong_source", "boolean_pid", "float_pid"])
def test_claim_cannot_replay_or_select_mutable_foreign_admission(case):
    admission = admitted()
    claimed = claim(admission)
    events = [admission, claimed]
    if case == "duplicate_claim":
        events.append(copy.deepcopy(claimed))
    else:
        field, value = {
            "wrong_admission": ("admission_digest", "0" * 64),
            "wrong_source": ("source_digest", "0" * 64),
            "boolean_pid": ("owner_pid", True),
            "float_pid": ("owner_pid", 123.0),
        }[case]
        claimed["payload"][field] = value
    with pytest.raises(ValueError):
        derive(events)


@pytest.mark.parametrize("case", ["missing_claim", "wrong_claim", "double_terminal", "delivery_success", "extra_field"])
def test_terminal_cannot_invent_result_or_settle_twice(case):
    admission = admitted()
    claimed = claim(admission)
    terminal = result(claimed)
    events = [admission, claimed, terminal]
    if case == "missing_claim":
        events.pop(1)
    elif case == "wrong_claim":
        terminal["payload"]["claim_digest"] = "0" * 64
    elif case == "double_terminal":
        events.append(result(claimed, "failed"))
    elif case == "delivery_success":
        terminal["payload"]["result"]["delivery_verified"] = True
    else:
        terminal["payload"]["result"]["unreviewed_grant"] = True
    with pytest.raises(ValueError):
        derive(events)


def test_cancel_before_dispatch_is_terminal_without_a_fake_claim_or_callback():
    admission = admitted()
    cancelled = event(
        "child_cancelled",
        {"child_run_id": CHILD, "claim_digest": None, "result": None},
        2,
    )
    assert derive([admission, cancelled])[CHILD]["state"] == "cancelled"
    with pytest.raises(ValueError):
        derive([admission, cancelled, claim(admission)])


def test_unrelated_budget_and_wait_events_do_not_change_child_completion():
    admission = admitted()
    claimed = claim(admission)
    events = [admission, event("budget_reserved", {}, 2), claimed, event("wait_observed", {}, 4), result(claimed)]
    before = copy.deepcopy(events)
    assert derive(events)[CHILD]["state"] == "completed"
    assert events == before


def test_mode_only_proposal_is_retained_as_an_actual_owned_source_change():
    admission = admitted()
    admission["payload"]["request"].update(mode="proposal", write_paths=["script.sh"])
    claimed = claim(admission)
    terminal = result(claimed)
    terminal["payload"]["result"]["changes"] = [
        {"path": "script.sh", "base_sha256": "a" * 64, "sha256": "a" * 64, "base_executable": False, "executable": True}
    ]
    assert derive([admission, claimed, terminal])[CHILD]["changes"][0]["executable"] is True
