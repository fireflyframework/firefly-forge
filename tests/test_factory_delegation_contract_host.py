# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Delegated work inherits immutable approval and returns proposals without delivery claims."""

import copy

import pytest

from firefly_forge.delegation_contract import child_run, deterministic_join, team_capture
from firefly_forge.delegation_models import DelegationStartInput
from firefly_forge.journal import digest

ROOT, CHILD = "a" * 32, "b" * 32


def parent():
    return {
        "id": ROOT,
        "repository_id": "c" * 32,
        "repository_name": "Example",
        "brief": "Build approved API",
        "agent": "api",
        "backend": "none",
        "frontend": "react",
        "operation": "build",
        "publish": True,
        "work_duration": {"protocol": "forge-work-duration-v2", "timeout_seconds": None},
        "sandbox_snapshot": {"mode": "host", "configuration": {"command_limit": 22}},
        "tool_policy_snapshot": {"settings": {"calls_limit": 30, "per_tool_limit": 10}},
        "role_snapshot": {"roles": [{"id": "implementer", "allowed_tools": ["read_file", "write_file"]}]},
        "policy_snapshot": {"digest": "d" * 64},
        "model_selection": {"model": "approved"},
        "config_revision": "e" * 32,
        "execution_actor": {"user_id": "f" * 32},
        "framework_knowledge_pin": {"digest": "1" * 64},
        "knowledge_context": "Frozen project knowledge",
        "status": "developing",
        "attempt": 2,
        "implementation_rounds": 1,
        "worker_response": {"old": "must not become child authority"},
        "cancel_request": {"status": "historical"},
        "git_delivery_policy": {"enabled": True},
        "pr_url": "https://example.invalid/old",
        "usage": {"requests": 10},
    }


def test_new_admissions_capture_existing_shared_limits_and_legacy_does_not_acquire_teams():
    run = parent()
    team = team_capture(run)
    assert team["budget"] == {"requests": 100, "tool_calls": 300, "commands": 22, "broker_calls": 30}
    assert team["broker_per_tool_limit"] == 10
    del run["work_duration"]
    assert team_capture(run) is None


def test_child_uses_exact_frozen_parent_authority_but_gets_a_fresh_private_lifecycle():
    run = parent()
    run["factory_team"] = team_capture(run)
    before = copy.deepcopy(run)
    request = DelegationStartInput(request_id="1" * 32, task_id="inspect", brief="Inspect API")
    source = {"path": "/owned/source", "digest": "2" * 64, "revision": "3" * 40, "tree": "4" * 40, "files": {}}
    child = child_run(run, request, source)
    assert child["id"] != ROOT
    assert child_run(run, request, source)["id"] == child["id"]
    for field in (
        "execution_actor",
        "policy_snapshot",
        "role_snapshot",
        "model_selection",
        "config_revision",
        "knowledge_context",
        "work_duration",
        "sandbox_snapshot",
        "factory_team",
    ):
        assert child[field] == run[field]
        if isinstance(run[field], dict):
            assert child[field] is not run[field]
    assert child["status"] == "queued" and child["publish"] is False
    assert child["factory_context"]["parent_run_id"] == ROOT
    assert child["factory_context"]["parent_attempt"] == 2
    assert (
        not {"worker_response", "git_delivery_policy", "cancel_request", "usage", "pr_url", "implementation_rounds"}
        & child.keys()
    )
    assert run == before


def result(identifier, task, path=None, base="0" * 64, content="fixed"):
    return {
        "delegation_id": identifier,
        "task_id": task,
        "state": "completed",
        "mode": "proposal" if path else "analysis",
        "summary": task,
        "changes": [] if path is None else [{"path": path, "base_sha256": base, "content": content}],
        "receipt_digest": digest({"id": identifier}),
    }


def test_join_is_ordered_independent_of_completion_order_and_never_applies_source():
    a, b = result("a" * 32, "api", "src/api.py"), result("b" * 32, "web", "src/web.ts")
    joined = deterministic_join([b, a], {"src/api.py": "0" * 64, "src/web.ts": "0" * 64})
    assert joined == deterministic_join([a, b], {"src/api.py": "0" * 64, "src/web.ts": "0" * 64})
    assert joined["status"] == "ready" and joined["applied"] is False
    assert [x["path"] for x in joined["changes"]] == ["src/api.py", "src/web.ts"]
    assert joined["delivery_verified"] is False


@pytest.mark.parametrize("case", ["shared", "parent_drift", "unknown", "duplicate", "failed"])
def test_join_holds_conflicts_and_unproved_or_incomplete_children(case):
    a, b = result("a" * 32, "a", "src/api.py"), result("b" * 32, "b", "src/web.ts")
    baseline = {"src/api.py": "0" * 64, "src/web.ts": "0" * 64}
    if case == "shared":
        b["changes"][0]["path"] = "src/api.py"
    elif case == "parent_drift":
        baseline["src/api.py"] = "1" * 64
    elif case == "unknown":
        b["state"] = "needs_reconciliation"
    elif case == "duplicate":
        b["delegation_id"] = a["delegation_id"]
    else:
        b["state"] = "failed"
    joined = deterministic_join([a, b], baseline)
    assert joined["status"] == "held" and joined["changes"] == []
    assert joined["holds"] and joined["applied"] is False


def test_sdk_and_broker_envelopes_remain_distinct_existing_approval_layers():
    captured = team_capture(parent())
    assert captured["budget"]["tool_calls"] == 300
    assert captured["budget"]["broker_calls"] == 30
    assert captured["broker_per_tool_limit"] == 10


def test_project_approved_nullable_sdk_budget_is_frozen_without_lifting_other_layers():
    run = parent()
    run["component_snapshot"] = {
        "project_factory_team": {
            "protocol": "forge-factory-team-v1",
            "automatic": True,
            "budget": {"requests": None, "tool_calls": 2**40, "commands": None, "broker_calls": None},
            "broker_per_tool_limit": None,
        }
    }
    captured = team_capture(run)
    assert captured["budget"] == {"requests": None, "tool_calls": 2**40, "commands": 22, "broker_calls": 30}
    assert captured["broker_per_tool_limit"] == 10


def test_child_preserves_parent_framework_target_without_reusing_its_run_scoped_receipt():
    run = parent()
    run["factory_team"] = team_capture(run)
    run["framework_knowledge_pin"] = {
        "run_id": ROOT,
        "source_commit": "a" * 40,
        "corpus_ref": "frozen-corpus",
        "digest": "1" * 64,
    }
    run["knowledge_context_pin_digest"] = "1" * 64
    request = DelegationStartInput(request_id="1" * 32, task_id="inspect", brief="Inspect API")
    child = child_run(
        run, request, {"path": "/owned/source", "digest": "2" * 64, "revision": "3" * 40, "tree": "4" * 40, "files": {}}
    )
    assert "framework_knowledge_pin" not in child
    assert "knowledge_context_pin_digest" not in child
    assert child["factory_context"]["parent_framework_pin"] == run["framework_knowledge_pin"]
    assert child["factory_context"]["parent_knowledge_context_pin_digest"] == "1" * 64
