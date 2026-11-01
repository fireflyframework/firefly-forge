# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fresh verification has an independent finite ledger, never renewed coding capacity."""

import copy

import pytest

from firefly_forge.execution_requests import digest


def operation(effect_budget=3):
    commands = [{"argv": ["approved-check", str(index)], "timeout": 10} for index in range(effect_budget)]
    return {
        "id": "a" * 32,
        "run_id": "b" * 32,
        "kind": "linked_verify",
        "request_digest": "c" * 64,
        "commands": commands,
        "commands_digest": digest({"commands": commands}),
        "effect_budget": effect_budget,
        "inputs_digest": "d" * 64,
    }


def append(events, kind, evidence):
    entry = {
        "request_id": "a" * 32,
        "sequence": len(events) + 1,
        "previous_hash": events[-1]["digest"] if events else "0" * 64,
        "kind": kind,
        "evidence": evidence,
        "created_at": "2026-11-01T00:00:00Z",
    }
    entry["digest"] = digest(entry)
    events.append(entry)
    return entry


def admission():
    events = []
    append(events, "linked_verification_admitted", {"request_digest": "c" * 64})
    append(events, "linked_verification_claimed", {"request_digest": "c" * 64})
    return events


def command(events, ordinal, *, exit_code=0, effect_budget=3):
    value = operation(effect_budget)
    intent = append(
        events,
        "linked_verification_command_intent",
        {
            "ordinal": ordinal,
            "command_digest": digest(value["commands"][ordinal - 1]),
            "inputs_digest": value["inputs_digest"],
        },
    )
    append(
        events,
        "linked_verification_command_result",
        {
            "ordinal": ordinal,
            "intent_digest": intent["digest"],
            "exit_code": exit_code,
            "output": "Fictional check result",
        },
    )


def test_independent_verification_consumes_each_declared_effect_once_and_requires_all_results():
    from firefly_forge.linked_verification_models import verification_progress

    value, events = operation(), admission()
    original = copy.deepcopy(value)
    assert verification_progress(value, events).next_ordinal == 1
    for ordinal in (1, 2, 3):
        command(events, ordinal)
        progress = verification_progress(value, events)
        assert progress.started == progress.completed == ordinal
        assert progress.ready_to_settle is (ordinal == 3)
        assert progress.next_ordinal == (ordinal + 1 if ordinal < 3 else None)
    assert value == original


def test_pending_effect_is_unknown_and_never_becomes_another_dispatch():
    from firefly_forge.linked_verification_models import verification_progress

    value, events = operation(), admission()
    append(
        events,
        "linked_verification_command_intent",
        {
            "ordinal": 1,
            "command_digest": digest(value["commands"][0]),
            "inputs_digest": value["inputs_digest"],
        },
    )
    progress = verification_progress(value, events)
    assert progress.started == 1 and progress.completed == 0
    assert progress.next_ordinal is None and progress.unknown and not progress.ready_to_settle
    append(
        events,
        "linked_verification_command_intent",
        {
            "ordinal": 1,
            "command_digest": digest(value["commands"][0]),
            "inputs_digest": value["inputs_digest"],
        },
    )
    with pytest.raises(ValueError):
        verification_progress(value, events)


def test_failed_verification_cannot_settle_or_dispatch_a_repair():
    from firefly_forge.linked_verification_models import verification_progress

    value, events = operation(), admission()
    command(events, 1, exit_code=1)
    progress = verification_progress(value, events)
    assert progress.failed and progress.next_ordinal is None and not progress.ready_to_settle
    command(events, 2)
    with pytest.raises(ValueError):
        verification_progress(value, events)


@pytest.mark.parametrize("change", ["budget", "request", "order", "command", "inputs", "receipt", "foreign_kind"])
def test_fresh_verification_refuses_changed_grants_inputs_and_foreign_events(change):
    from firefly_forge.linked_verification_models import verification_progress

    value, events = operation(), admission()
    command(events, 1)
    if change == "budget":
        value["effect_budget"] += 1
    elif change == "request":
        events[-1]["request_id"] = "f" * 32
    elif change == "order":
        events[-2]["evidence"]["ordinal"] = 2
    elif change == "command":
        events[-2]["evidence"]["command_digest"] = "f" * 64
    elif change == "inputs":
        events[-2]["evidence"]["inputs_digest"] = "f" * 64
    elif change == "receipt":
        events[-1]["evidence"]["intent_digest"] = "f" * 64
    else:
        events[-1]["kind"] = "coding_dispatch"
    # Keep native chain integrity valid so semantic refusal cannot rely on a stale hash.
    previous = "0" * 64
    for event in events:
        event["previous_hash"] = previous
        event["digest"] = previous = digest({k: v for k, v in event.items() if k != "digest"})
    with pytest.raises(ValueError):
        verification_progress(value, events)


@pytest.mark.parametrize(
    "field",
    [
        "version",
        "acknowledge_all_fresh_checks",
        "acknowledge_no_coding_or_repair",
        "acknowledge_original_history_and_budget_preserved",
    ],
)
def test_new_verification_consent_requires_exact_version_and_true_booleans(field):
    from firefly_forge.linked_verification_models import LinkedVerificationInputV1

    value = {
        "version": 1,
        "request_id": "a" * 32,
        "candidate_digest": "b" * 64,
        "acknowledge_all_fresh_checks": True,
        "acknowledge_no_coding_or_repair": True,
        "acknowledge_original_history_and_budget_preserved": True,
    }
    assert LinkedVerificationInputV1.model_validate(value).request_id == "a" * 32
    for replacement in [True, 1.0, "1"] if field == "version" else [1, "true", False]:
        with pytest.raises(ValueError):
            LinkedVerificationInputV1.model_validate({**value, field: replacement})


def test_fifty_one_declared_commands_roundtrip_and_settle_without_a_new_fixed_cap():
    from linked_verification_fixtures import candidate, receipt

    from firefly_forge.linked_verification_models import LinkedVerificationReceiptV1, verification_progress

    value, events = operation(51), admission()
    for ordinal in range(1, 52):
        command(events, ordinal, effect_budget=51)
    progress = verification_progress(value, events)
    assert len(events) == 104 and progress.started == progress.completed == 51
    assert progress.ready_to_settle and progress.next_ordinal is None
    wire = receipt(candidate(51), status="completed")
    parsed = LinkedVerificationReceiptV1.model_validate_json(
        LinkedVerificationReceiptV1.model_validate(wire).model_dump_json()
    )
    assert parsed.candidate.effect_budget == parsed.settlement.checks_count == 51
    assert parsed.candidate.original_effect_budget == 22 and parsed.candidate.original_effect_count == 18


@pytest.mark.parametrize(
    ("status", "change"),
    [
        ("completed", {"next_ordinal": 1}),
        ("completed", {"ready_to_settle": True}),
        ("queued", {"failed": True}),
        ("queued", {"next_ordinal": 1}),
        ("needs_reconciliation", {"next_ordinal": 1}),
        ("needs_reconciliation", {"ready_to_settle": True}),
    ],
)
def test_receipt_refuses_status_and_progress_contradictions(status, change):
    from linked_verification_fixtures import receipt

    from firefly_forge.linked_verification_models import LinkedVerificationReceiptV1

    value = receipt(status=status)
    value["progress"].update(change)
    with pytest.raises(ValueError):
        LinkedVerificationReceiptV1.model_validate(value)
