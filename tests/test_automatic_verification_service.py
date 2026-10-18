# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_forge.configuration import ConfigurationStore
from firefly_forge.journal import digest
from firefly_forge.sandbox_models import capture_sandbox
from firefly_forge.tool_broker import tool_policy_snapshot


@pytest.fixture
def source(tmp_path):
    configuration = ConfigurationStore(tmp_path / "configuration")
    workspace = tmp_path / "workspaces" / uuid4().hex
    workspace.mkdir(parents=True)
    (workspace / "app.py").write_text("print('existing source')\n")
    store = SimpleNamespace(configuration=configuration, credential_registry=dict)
    run = {
        "id": workspace.name,
        "repository_id": uuid4().hex,
        "agent": "codex",
        "commands": [{"argv": ["check"], "timeout": 30}],
        "sandbox_snapshot": capture_sandbox(None),
        "tool_policy_snapshot": tool_policy_snapshot(configuration),
        "worker_request": {
            "id": "not-a-magic-name",
            "kind": "verification",
            "status": "pending",
            "task_id": "implement",
            "attempt": 1,
        },
        "status": "paused",
        **configuration.snapshot("codex"),
    }
    return store, run, workspace


def test_eligible_automatic_request_uses_typed_kind_not_its_text(source):
    from firefly_forge.verification_handoffs import automatic_candidate

    _, run, _ = source
    assert automatic_candidate(run)
    run["worker_request"]["kind"] = "approval"
    run["worker_request"]["question"] = "Please run verification commands automatically"
    assert not automatic_candidate(run)


def test_legacy_absent_policy_is_manual_even_with_new_current_defaults(source):
    from firefly_forge.verification_handoffs import automatic_candidate

    _, run, _ = source
    run["tool_policy_snapshot"] = None
    assert not automatic_candidate(run)


def test_pre_admission_refusal_is_not_dispatched_forever(source):
    from firefly_forge.verification_handoffs import automatic_candidate

    _, run, _ = source
    run["verification_refusal"] = {"version": 1, "request_digest": digest(run["worker_request"])}
    assert not automatic_candidate(run)


@pytest.mark.parametrize("cache", [".pytest_cache", "frontend/.cache"])
def test_generated_check_caches_do_not_change_the_bound_application_source(source, cache):
    from firefly_forge.verification_handoffs import verification_intent
    from firefly_forge.verification_models import check_verification_cut

    store, run, workspace = source
    intent = verification_intent(store, run, workspace, [], [])
    (workspace / "frontend").mkdir()
    intent = verification_intent(store, run, workspace, [], [])
    (workspace / cache).mkdir(parents=True)
    (workspace / cache / "cache").write_text("test cache")
    check_verification_cut(store, run, intent)


def test_frozen_intent_binds_untracked_source_commands_policy_and_provider(source):
    from firefly_forge.verification_handoffs import verification_intent
    from firefly_forge.verification_models import check_verification_cut

    store, run, workspace = source
    intent = verification_intent(store, run, workspace, [], [])
    check_verification_cut(store, run, intent)
    (workspace / "new.py").write_text("print('late source')\n")
    with pytest.raises(ValueError, match="source changed"):
        check_verification_cut(store, run, intent)


def test_ordinals_avoid_prior_native_effects_and_do_not_reset_total_budget(source):
    from firefly_forge.verification_handoffs import verification_intent

    store, run, workspace = source
    existing = [{"task_id": "implement", "parent_attempt": 1, "ordinal": 1}]
    intent = verification_intent(store, run, workspace, [], existing)
    assert intent["ordinals"] == [2]
    run["sandbox_snapshot"] = capture_sandbox({"command_limit": 1})
    with pytest.raises(ValueError, match="budget"):
        verification_intent(store, run, workspace, [], existing)


def test_three_continuations_do_not_expand_four_attempt_role_limit(source):
    from firefly_forge.verification_handoffs import verification_intent

    store, run, workspace = source
    run["worker_request"]["attempt"] = 4
    with pytest.raises(ValueError, match="attempt"):
        verification_intent(store, run, workspace, [], [])


def test_legacy_adoption_is_exact_current_provider_cut_not_frozen_consumed_guard(source):
    from firefly_forge.verification_handoffs import verification_intent
    from firefly_forge.verification_models import VerificationAdoption, check_verification_cut, verification_provider

    store, run, workspace = source
    from firefly_forge.verification_models import verification_source

    adoption = VerificationAdoption(
        request_id=uuid4().hex,
        pending_request_digest=digest(run["worker_request"]),
        source_digest=verification_source(workspace),
        commands_digest=digest({"commands": run["commands"]}),
        policy_digest=tool_policy_snapshot(store.configuration)["digest"],
        provider_digest=verification_provider(store, run, "codex"),
        agent="codex",
    )
    run["tool_policy_snapshot"] = None
    run["verification_adoption"] = {
        "request": adoption.model_dump(),
        "policy_snapshot": tool_policy_snapshot(store.configuration),
    }
    intent = verification_intent(store, run, workspace, [], [])
    assert intent["agent"] == "codex"
    assert intent["provider_digest"] == adoption.provider_digest
    changed = copy.deepcopy(run)
    changed["commands"][0]["argv"] = ["other"]
    with pytest.raises(ValueError, match="changed"):
        check_verification_cut(store, changed, intent)
