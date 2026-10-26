# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fictional IDs mirror the retained paused factory-RED verification shape."""

import copy
import json

import pytest
from communication_model_fixture import reviewed_model
from test_communication_writer import configured_store

from firefly_forge.communication import CommunicationCard, CommunicationWriter, _validate_draft, communication_facts

REASON = "Run the new backend regression to record the expected validation failure before changing the handler."
CONTEXT = "The regression is written. Implementation is paused until the factory records verification."
HOLD = "Verification needs explicit reconciliation or renewed authority before continuation."


def paused(handoff="held"):
    value = {
        "id": "8" * 32,
        "status": "paused",
        "operation": "maintain",
        "active_task": "implement",
        "attempt": 2,
        "brief": "Correct native validation responses after proving the regression fails.",
        "component_snapshot": {"name": "Orbit CRM", "responsibility": "Company and contact workflows."},
        "source_checkpoint_snapshot": {"revision": "b" * 40},
        "worker_request": {
            "id": "factory-red-check",
            "kind": "verification",
            "question": REASON,
            "context": CONTEXT,
            "choices": [],
            "status": "pending",
            "task_id": "implement",
            "attempt": 1,
        },
        "final_verification": [{"exit_code": 1, "attempt": 1}],
    }
    if handoff:
        value["verification_handoff"] = {
            "version": 1,
            "id": "a" * 32,
            "request_id": "factory-red-check",
            "status": handoff,
            "commands_count": 7,
            "completed_commands": 0,
            "failed_commands": 0,
            "source_digest": "c" * 64,
        }
        if handoff == "held":
            value["verification_handoff"]["error"] = HOLD
    return value


@pytest.mark.parametrize("state", [None, "pending", "running", "held"])
def test_factory_request_has_recorded_reason_and_next_action_without_human_question(state):
    value = paused(state)
    before = copy.deepcopy(value)
    facts = communication_facts(value)
    action = facts["factory_verification"]
    assert action["responsibility"] == "factory" and action["requires_human_answer"] is False
    assert action["source"] == "recorded_worker_request"
    assert action["reason"] == REASON and action["context"] == CONTEXT
    assert action["handoff_status"] == (state or "not_recorded")
    assert action["execution_grant"] is False and action["next_native_action"]
    assert action["checks"]["result"] == "not_completed"
    assert "decision" not in facts and "check_batch" not in facts
    assert value == before
    if state == "held":
        assert action["hold"] == {"source": "recorded_handoff_error", "reason": HOLD}
        assert action["checks"]["recorded"] == 0
    else:
        assert action["checks"]["recorded"] is None


def test_completed_handoff_result_does_not_relabel_retained_old_checks_as_current():
    value = paused("completed")
    value["verification_handoff"].update(completed_commands=7, failed_commands=1)
    value["worker_request"].update(status="answered", response_source="factory_verification")
    facts = communication_facts(value)
    action = facts["factory_verification"]
    assert action["checks"] == {
        "source": "recorded_handoff",
        "result": "completed",
        "total": 7,
        "recorded": 7,
        "failed": 1,
    }
    assert action["next_native_action"] == "review_recorded_verification_results"
    assert facts["verification"]["provenance"] == "retained"
    assert facts["check_status"] == {"current_attempt": 2, "recorded": 0, "provenance": "not_recorded"}
    assert "decision" not in facts


def test_generic_state_update_cannot_omit_available_factory_reason():
    facts = {"state": {"status": "paused"}, "factory_verification": {"reason": REASON}}
    generic = CommunicationCard.model_validate(
        {
            "title": {"text": "Work paused", "fact_ids": ["state"]},
            "paragraphs": [{"text": "No reason was provided.", "fact_ids": ["state"]}],
        }
    )
    with pytest.raises(ValueError, match="context"):
        _validate_draft(generic, facts)


async def test_same_factory_reason_reaches_writer_and_critic_before_one_ai_repair(tmp_path):
    value = paused()
    bad = {
        "title": {"text": "Work paused", "fact_ids": ["state", "work"]},
        "paragraphs": [
            {"text": "No reason was provided.", "fact_ids": ["factory_verification", "requested_work"]},
            {"text": "Saved results belong to the earlier attempt.", "fact_ids": ["check_status", "verification"]},
        ],
        "decision": None,
    }
    corrected = {
        "title": {"text": "Orbit CRM is waiting for factory verification", "fact_ids": ["work", "state"]},
        "paragraphs": [
            {
                "text": (
                    "The requested handler correction is paused while the factory checks the new regression. "
                    "The verification handoff is held for reconciliation or renewed authority before continuation."
                ),
                "fact_ids": ["requested_work", "factory_verification"],
            },
            {
                "text": (
                    "That handoff has no completed check result. The earlier failed check belongs to a previous "
                    "attempt. The next step is to review the saved verification hold before any further execution."
                ),
                "fact_ids": ["factory_verification", "verification", "check_status"],
            },
        ],
        "decision": None,
    }
    calls = []
    model = reviewed_model(
        bad, verdict="unsupported", issues=["insufficient_context"], correction=corrected, calls=calls
    )
    result = await CommunicationWriter(await configured_store(tmp_path), model=model).write(
        value, "State-only fallback", lambda text: text
    )
    assert result["communication"]["mode"] == "ai"
    assert result["communication"]["repair_attempted"] is True
    assert [phase for phase, _, _ in calls] == ["draft", "review", "draft"]
    assert "waiting for factory verification" in result["body"]
    assert "No reason was provided" not in result["body"]
    serialized = json.dumps([(str(messages), str(info)) for _, messages, info in calls])
    assert REASON in serialized and CONTEXT in serialized and HOLD in serialized
    assert result["communication"]["facts"]["factory_verification"]["execution_grant"] is False
