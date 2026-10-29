# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Work duration is optional; ownership and retained source-reader limits remain separate."""

import copy
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from firefly_forge.runner_pool_models import FactoryFence
from firefly_forge.tool_operation_models import OwnedToolOperationV2, operation_policy
from firefly_forge.work_duration_models import WorkDurationPolicyV2


def selected(chosen):
    policy = WorkDurationPolicyV2(timeout_seconds=chosen).model_dump(mode="json")
    return {"work_duration": policy, "sandbox_snapshot": {"configuration": {"work_duration": copy.deepcopy(policy)}}}


def value(chosen=None):
    return {
        "version": 2,
        "protocol": "forge-owned-tool-operation-v2",
        "generation": "a" * 32,
        "task_key": "b" * 32,
        "intent_digest": "d" * 64,
        "task_id": "implement",
        "attempt": 1,
        "factory_fence": FactoryFence(
            factory_schema="owned_test", pid=42, backend_started_at=datetime(2026, 10, 29, tzinfo=UTC)
        ),
        "run_cut": "c" * 64,
        "work_duration": WorkDurationPolicyV2(timeout_seconds=chosen),
        "work_started_at_ms": 1000,
        "work_deadline_ms": None if chosen is None else 1000 + chosen * 1000,
        "owner_grace_seconds": 30,
    }


def test_legacy_absence_and_corrective_source_readers_do_not_acquire_new_operation_semantics():
    assert operation_policy({}) is None
    assert operation_policy(selected(None)) == WorkDurationPolicyV2()
    assert operation_policy(selected(864000)) == WorkDurationPolicyV2(timeout_seconds=864000)
    assert operation_policy(selected(None), source_read=True) is None


def test_nullable_work_deadline_has_mandatory_finite_owned_grace_and_exact_identity():
    record = OwnedToolOperationV2.model_validate(value())
    assert record.work_deadline_ms is None and record.owner_grace_seconds == 30
    with pytest.raises(ValidationError):
        record.owner_grace_seconds = 0
    assert OwnedToolOperationV2.model_validate(value(2**40)).work_deadline_ms == 1000 + 2**40 * 1000


@pytest.mark.parametrize(
    "changed",
    [
        {"version": 2.0},
        {"attempt": True},
        {"owner_grace_seconds": None},
        {"owner_grace_seconds": 0},
        {"work_deadline_ms": 999999},
        {"generation": "foreign"},
    ],
)
def test_operation_cannot_rewrite_owner_identity_or_renew_its_optional_work_deadline(changed):
    with pytest.raises(ValidationError):
        OwnedToolOperationV2.model_validate({**value(), **changed})


def test_mismatched_top_level_and_runner_work_policy_refuses_before_operation_admission():
    run = selected(None)
    run["sandbox_snapshot"]["configuration"]["work_duration"]["timeout_seconds"] = 1
    with pytest.raises(ValueError):
        operation_policy(run)


def test_operation_pins_its_original_native_task_intent_not_only_a_mutable_task_key():
    record = OwnedToolOperationV2.model_validate({**value(), "intent_digest": "d" * 64})
    assert record.intent_digest == "d" * 64
