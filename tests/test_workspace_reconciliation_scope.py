# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""An environment transition cannot manufacture a new provider or runner allowance."""

import copy
import importlib
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest


def implementation():
    module = importlib.import_module("firefly_forge.workspace_reconciliation_models")
    return module


@pytest.fixture
def queued_cut():
    run = {
        "id": UUID(int=1).hex,
        "status": "failed",
        "active_task": "implement",
        "error": "Generated changes contain private credentials or worker metadata; inspect the workspace",
        "attempt": 1,
        "implementation_rounds": 1,
        "repair_budget": 2,
        "verification_handoff": {"id": "handoff", "status": "completed"},
        "worker_response": {"verification_handoff_id": "handoff"},
    }
    task = {"id": "implement", "status": "queued", "attempt": 2, "idempotency_key": "previous"}
    entries = [
        {"kind": "verification_handoff_outcome", "payload": {"id": "handoff", "status": "completed"}},
        {"kind": "resume", "payload": {"automatic": True, "verification_handoff_id": "handoff"}},
    ]
    return run, task, entries


def test_existing_queued_task_and_budget_are_preserved(queued_cut):
    run, task, entries = queued_cut
    before = copy.deepcopy(queued_cut)
    result = implementation().queued_environment_scope(
        run, task, entries, verification_allowance=1, human_allowance=0, remaining=28, final_checks=7
    )
    assert result == {"attempt": 2, "implementation_limit": 4, "remaining_commands": 28, "final_checks": 7}
    assert queued_cut == before


@pytest.mark.parametrize(
    "change",
    [
        "not_failed",
        "wrong_failure",
        "task_failed",
        "attempt_five",
        "intent",
        "native_intent",
        "unknown",
        "no_handoff",
        "budget",
        "limit",
    ],
)
def test_scope_refuses_recovery_replay_unknown_effects_and_budget_expansion(queued_cut, change):
    run, task, entries = queued_cut
    remaining = 28
    if change == "not_failed":
        run["status"] = "paused"
    elif change == "wrong_failure":
        run["error"] = "Provider outcome unknown"
    elif change == "task_failed":
        task["status"] = "failed"
    elif change == "attempt_five":
        task["attempt"], run["implementation_rounds"] = 5, 4
    elif change == "intent":
        entries.append({"kind": "provider_dispatch_intent", "payload": {"attempt": 2}})
    elif change == "native_intent":
        entries.append(
            {
                "kind": "intent",
                "task_id": "implement",
                "payload": {},
                "idempotency_key": uuid5(NAMESPACE_URL, f"forge:{run['id']}:implement:2").hex,
            }
        )
    elif change == "unknown":
        run["bridge_completion_hold"] = {"status": "unknown"}
    elif change == "no_handoff":
        entries.pop()
    elif change == "budget":
        remaining = 6
    else:
        run["repair_budget"] = 0
        task["attempt"], run["implementation_rounds"] = 3, 2
    with pytest.raises(ValueError):
        implementation().queued_environment_scope(
            run, task, entries, verification_allowance=1, human_allowance=0, remaining=remaining, final_checks=7
        )


@pytest.mark.parametrize("field", ["acknowledge_environment_only", "acknowledge_resume_existing_task"])
@pytest.mark.parametrize("value", [False, 1, "true"])
def test_reconciliation_requires_exact_explicit_acknowledgements(field, value):
    body = {
        "request_id": "a" * 32,
        "candidate_digest": "b" * 64,
        "acknowledge_environment_only": True,
        "acknowledge_resume_existing_task": True,
    }
    body[field] = value
    with pytest.raises(ValueError):
        implementation().WorkspaceReconciliationInput.model_validate(body)
