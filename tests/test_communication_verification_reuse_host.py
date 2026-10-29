# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""AI wording must distinguish saved check results from fresh commands."""

import pytest

from firefly_forge.communication import communication_facts


def reused():
    return {
        "execution": "reused",
        "argv": ["python", "-m", "pytest"],
        "exit_code": 0,
        "output": "one passed",
        "created_at": "2026-10-29T04:00:00Z",
        "attempt": 2,
        "source_execution_id": "a" * 32,
        "source_record_digest": "b" * 64,
        "verification_reuse_intent_digest": "c" * 64,
    }


def facts(checks):
    return communication_facts(
        {
            "id": "d" * 32,
            "status": "validating",
            "attempt": 3,
            "component_snapshot": {"name": "Example API", "responsibility": "Manage contacts"},
            "final_verification": checks,
        }
    )


def test_mixed_checks_expose_six_reused_results_and_one_new_execution():
    value = facts([reused() for _ in range(6)] + [{"attempt": 3, "exit_code": 0}])
    assert value["verification"] == {
        "recorded": 7,
        "passed": 7,
        "failed": 0,
        "provenance": "retained",
        "reused_results": 6,
        "new_executions": 1,
    }
    assert value["check_status"] == {"current_attempt": 3, "recorded": 1, "provenance": "current"}


def test_reused_result_never_becomes_new_execution_by_its_attempt_label():
    check = {**reused(), "attempt": 3}
    value = facts([check])
    assert value["verification"]["provenance"] == "retained"
    assert value["verification"]["new_executions"] == 0
    assert value["check_status"] == {"current_attempt": 3, "recorded": 0, "provenance": "not_recorded"}


@pytest.mark.parametrize(
    ("key", "invalid"),
    [
        ("exit_code", 1),
        ("source_execution_id", "missing"),
        ("source_record_digest", "wrong"),
        ("verification_reuse_intent_digest", "wrong"),
        ("created_at", "unknown"),
        ("output", None),
        ("argv", []),
        ("execution", "unconfirmed"),
    ],
)
def test_incomplete_reuse_custody_keeps_execution_provenance_unknown(key, invalid):
    value = facts([{**reused(), "attempt": 3, key: invalid}])
    assert value["verification"]["provenance"] == "unknown"
    assert "new_executions" not in value["verification"]
    assert value["check_status"] == {"current_attempt": 3, "recorded": None, "provenance": "unknown"}
