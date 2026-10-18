# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from firefly_forge.provider_recovery_models import (
    LegacyProcessAttestation,
    ProviderProcessOutcome,
    ProviderRecoveryAdmission,
    dispatch_keys,
    failed_implementation,
)


def attestation():
    return {
        "provenance": "legacy_operator_attestation",
        "evidence_digest": "e" * 64,
        "trusted_operator_observation": True,
        "process_terminated": True,
        "process_group_drained": True,
        "no_accepted_final_result": True,
        "exit_code": -25,
    }


def test_legacy_attestation_is_explicit_and_never_native_process_evidence():
    value = LegacyProcessAttestation.model_validate(attestation())
    assert value.provenance == "legacy_operator_attestation" and value.exit_code == -25
    body = ProviderRecoveryAdmission(
        request_id="a" * 32, candidate_digest="b" * 64, agent="codex", legacy_attestation=value
    )
    assert body.native_process_digest is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("provenance", "native_cli"),
        ("trusted_operator_observation", False),
        ("trusted_operator_observation", "true"),
        ("process_terminated", False),
        ("process_group_drained", False),
        ("no_accepted_final_result", False),
        ("exit_code", 0),
        ("exit_code", True),
        ("evidence_digest", "not-a-digest"),
    ],
)
def test_legacy_unknown_or_successful_process_cannot_authorize_recovery(field, value):
    with pytest.raises(ValidationError):
        LegacyProcessAttestation.model_validate({**attestation(), field: value})


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": "new-id"},
        {"candidate_digest": "bad"},
        {"agent": "bridge"},
        {"status": "queued"},
        {"repair_budget": 4},
        {"native_process_digest": "c" * 64},
    ],
)
def test_admission_forbids_replacement_identity_fallback_and_process_proof_ambiguity(change):
    body = {"request_id": "a" * 32, "candidate_digest": "b" * 64, "agent": "codex", "legacy_attestation": attestation()}
    with pytest.raises(ValidationError):
        ProviderRecoveryAdmission.model_validate({**body, **change})


def test_native_process_evidence_requires_typed_exit_and_actual_group_absence():
    outcome = ProviderProcessOutcome(exit_code=-25, leader_waited=True, streams_drained=True, group_absent=True)
    assert outcome.known_failure
    assert not outcome.model_copy(update={"group_absent": False}).known_failure
    assert not outcome.model_copy(update={"streams_drained": False}).known_failure
    assert not outcome.model_copy(update={"exit_code": 0}).known_failure
    with pytest.raises(ValidationError):
        ProviderProcessOutcome(exit_code=True, leader_waited=True, streams_drained=True, group_absent=True)


def test_dispatch_budget_counts_unreceipted_and_failed_intents_without_double_counting_new_receipts():
    entries = [
        {"kind": "intent", "task_id": "implement", "idempotency_key": "one", "payload": {}},
        {"kind": "intent", "task_id": "implement", "idempotency_key": "two", "payload": {}},
        {
            "kind": "provider_dispatch_intent",
            "task_id": "implement",
            "idempotency_key": "two:provider",
            "payload": {"task_key": "two"},
        },
        {"kind": "intent", "task_id": "verify", "idempotency_key": "qa", "payload": {}},
    ]
    assert dispatch_keys(entries) == {"one", "two"}
    assert (
        len(
            dispatch_keys(
                entries
                + [
                    {
                        "kind": "provider_dispatch_intent",
                        "task_id": "implement",
                        "idempotency_key": "three",
                        "payload": {"task_key": "three"},
                    }
                ]
            )
        )
        == 3
    )


def failed():
    run = {"status": "failed", "agent": "codex", "active_task": "implement", "attempt": 2}
    task = {"id": "implement", "status": "running", "attempt": 2, "idempotency_key": "two"}
    entries = [{"kind": "intent", "task_id": "implement", "idempotency_key": "two", "digest": "d" * 64, "payload": {}}]
    return run, task, entries


def test_legacy_stale_running_task_is_bound_to_its_actual_failed_run_not_reclassified_as_running_work():
    run, task, entries = failed()
    original = copy.deepcopy((run, task, entries))
    assert failed_implementation(run, task, entries)["idempotency_key"] == "two"
    assert (run, task, entries) == original


@pytest.mark.parametrize(
    "change",
    [
        {"status": "developing"},
        {"status": "succeeded"},
        {"agent": "bridge"},
        {"cancel_request": {"status": "requested"}},
        {"bridge_completion_hold": {"id": "held"}},
    ],
)
def test_nonfailed_unknown_or_cancelled_run_is_not_a_failed_provider_candidate(change):
    run, task, entries = failed()
    with pytest.raises(ValueError):
        failed_implementation({**run, **change}, task, entries)


def test_completed_old_intent_cannot_be_recovered_as_failed():
    run, task, entries = failed()
    entries.append({"kind": "receipt", "task_id": "implement", "idempotency_key": "two", "payload": {}})
    with pytest.raises(ValueError):
        failed_implementation(run, task, entries)
