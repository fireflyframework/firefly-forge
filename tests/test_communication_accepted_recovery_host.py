# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Accepted output supersedes its historical worker handoff, without granting QA."""

import copy

import pytest

from firefly_forge.communication import communication_facts


def accepted_run():
    return {
        "id": "1" * 32,
        "status": "paused",
        "attempt": 3,
        "operation": "maintain",
        "brief": "Correct native validation responses in the CRM.",
        "component_snapshot": {"name": "Orbit CRM", "responsibility": "Company and contact workflows."},
        "active_task": "implement",
        "implementation_pending": False,
        "error": "Consumed terminal original scope or invocation changed",
        "consumed_terminal_recovery": {
            "version": 2,
            "state": "accepted",
            "request_id": "2" * 32,
            "record_digest": "3" * 64,
            "claim_digest": "4" * 64,
        },
        "worker_request": {
            "id": "validation-checks",
            "kind": "verification",
            "question": "Verify the repaired validation handler.",
            "context": "The repair is written; check the retained source.",
            "choices": [],
            "status": "answered",
            "attempt": 2,
        },
        "verification_handoff": {
            "version": 1,
            "id": "5" * 32,
            "request_id": "validation-checks",
            "status": "held",
            "commands_count": 7,
            "completed_commands": 7,
            "failed_commands": 0,
            "error": "Verification needs explicit reconciliation or renewed authority before continuation.",
        },
    }


def test_accepted_recovery_is_visible_without_reviving_its_stale_error():
    run = accepted_run()
    before = copy.deepcopy(run)
    facts = communication_facts(run)
    recovery = facts["consumed_terminal_recovery"]
    assert recovery["state"] == "accepted"
    assert recovery["source"] == "recorded_run_projection_not_admission"
    assert recovery["execution_grant"] is False
    assert recovery["next_native_action"] == "inspect_independent_verification_without_repeating_recovery"
    assert "recorded_error" not in facts
    assert run == before


def test_accepted_result_labels_old_handoff_results_as_retained_not_current():
    facts = communication_facts(accepted_run())
    handoff = facts["factory_verification"]
    assert handoff["provenance"] == "retained_before_accepted_implementation"
    assert handoff["checks"]["result"] == "completed"
    assert handoff["checks"]["recorded"] == 7
    assert handoff["checks"]["failed"] == 0
    assert handoff["execution_grant"] is False
    assert "hold" not in handoff
    assert handoff["next_native_action"] == facts["consumed_terminal_recovery"]["next_native_action"]
    assert facts["check_status"]["recorded"] == 0


@pytest.mark.parametrize(
    "key,value", [("version", True), ("state", "completed"), ("state", []), ("claim_digest", "bad")]
)
def test_invalid_recovery_projection_cannot_supersede_the_handoff(key, value):
    run = accepted_run()
    run["consumed_terminal_recovery"][key] = value
    facts = communication_facts(run)
    assert "consumed_terminal_recovery" not in facts
    assert "provenance" not in facts["factory_verification"]
    assert "hold" in facts["factory_verification"]


def test_pending_implementation_cannot_be_advertised_as_accepted_from_an_old_marker():
    run = accepted_run()
    run["implementation_pending"] = True
    facts = communication_facts(run)
    assert "consumed_terminal_recovery" not in facts
    assert "provenance" not in facts["factory_verification"]


@pytest.mark.parametrize("attempt", [3, 4, None, True])
def test_accepted_marker_does_not_erase_a_current_or_unknown_verification_hold(attempt):
    run = accepted_run()
    run["worker_request"]["attempt"] = attempt
    facts = communication_facts(run)
    assert facts["consumed_terminal_recovery"]["state"] == "accepted"
    assert "hold" in facts["factory_verification"]
    assert "provenance" not in facts["factory_verification"]
    assert facts["factory_verification"]["next_native_action"] == "review_reconciliation_or_renewed_authority"
