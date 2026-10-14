# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import importlib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError


def module():
    return importlib.import_module("firefly_forge.learning_models")


def context(**updates):
    return {
        "repository_id": "a" * 32,
        "project_id": "b" * 32,
        "component_id": "stock",
        "framework": "pyfly",
        "version": "26.10.1",
        "dependency_digest": "c" * 64,
        "source_tree": "d" * 40,
        "query_digest": "e" * 64,
        **updates,
    }


def record(**updates):
    return {
        "id": "1" * 32,
        "state": "verified",
        "kind": "local_outcome",
        "context": context(),
        "verified_at": datetime(2026, 10, 13, tzinfo=UTC),
        "expires_at": datetime(2026, 10, 16, tzinfo=UTC),
        "receipt_verified": True,
        "unknown_effect": False,
        **updates,
    }


@pytest.mark.parametrize(
    "state,reason", [("candidate", "candidate_only"), ("invalidated", "invalidated"), ("superseded", "superseded")]
)
def test_state_never_bypasses_exact_evidence_gate(state, reason):
    m = module()
    assert (
        m.eligibility(
            m.LearningCandidate.model_validate(record(state=state)),
            m.LearningContext.model_validate(context()),
            datetime(2026, 10, 14, tzinfo=UTC),
        ).reason
        == reason
    )


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"repository_id": "f" * 32}, "scope_denied"),
        ({"project_id": "f" * 32}, "scope_denied"),
        ({"component_id": "other"}, "scope_denied"),
        ({"framework": "larafly"}, "version_drift"),
        ({"version": "26.10.2"}, "version_drift"),
        ({"dependency_digest": "f" * 64}, "context_drift"),
        ({"source_tree": "f" * 40}, "source_drift"),
        ({"query_digest": "f" * 64}, "context_drift"),
    ],
)
def test_semantic_similarity_cannot_override_scope_version_source_or_query(change, reason):
    m = module()
    decision = m.eligibility(
        m.LearningCandidate.model_validate(record()),
        m.LearningContext.model_validate(context(**change)),
        datetime(2026, 10, 14, tzinfo=UTC),
    )
    assert not decision.eligible and decision.reason == reason


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"receipt_verified": False}, "missing_receipts"),
        ({"unknown_effect": True}, "unknown_effect"),
        ({"kind": "web_reference"}, "candidate_only"),
    ],
)
def test_snippet_or_model_claim_cannot_supply_verified_local_procedure(change, reason):
    m = module()
    decision = m.eligibility(
        m.LearningCandidate.model_validate(record(**change)),
        m.LearningContext.model_validate(context()),
        datetime(2026, 10, 14, tzinfo=UTC),
    )
    assert not decision.eligible and decision.reason == reason


def test_exact_fresh_local_evidence_is_eligible_and_stale_requires_refresh():
    m = module()
    entry = m.LearningCandidate.model_validate(record())
    observed = m.LearningContext.model_validate(context())
    assert m.eligibility(entry, observed, datetime(2026, 10, 14, tzinfo=UTC)).reason == "verified_exact_match"
    stale = m.eligibility(entry, observed, datetime(2026, 10, 16, tzinfo=UTC))
    assert not stale.eligible and stale.reason == "expired" and stale.refresh_required


@pytest.mark.parametrize("purpose", ["verification", "regression", "diagnostic", "intentional_red"])
def test_repeat_control_never_skips_independent_checks(purpose):
    m = module()
    decision = m.repeat_decision(purpose, identical_verified_failures=9, max_repeated_attempts=2)
    assert decision.allowed and decision.reason == "independent_check_required"


def test_repeat_circuit_applies_only_to_unchanged_repair_generation():
    m = module()
    assert m.repeat_decision("repair_generation", identical_verified_failures=1, max_repeated_attempts=2).allowed
    blocked = m.repeat_decision("repair_generation", identical_verified_failures=2, max_repeated_attempts=2)
    assert not blocked.allowed and blocked.reason == "identical_attempt_exhausted"


def test_envelope_is_typed_bounded_and_has_no_model_verified_switch():
    m = module()
    envelope = {
        "version": 1,
        "type": "candidate",
        "correlation_id": "1" * 32,
        "run_id": "2" * 32,
        "task_id": "implement",
        "role": "implementer",
        "context": context(),
        "source": {"base_revision": "3" * 40, "head": "3" * 40, "source_tree": "d" * 40},
        "question": "How does this stock check behave?",
        "statement": "Observed local procedure candidate",
        "purpose": "verification",
    }
    assert m.LearningEnvelope.model_validate(envelope).type == "candidate"
    for changes in (
        {"verified": True},
        {"type": "verified_by_model"},
        {"purpose": "code_failure"},
        {"question": "x" * 4001},
        {"correlation_id": "not-stable"},
    ):
        with pytest.raises(ValidationError):
            m.LearningEnvelope.model_validate({**envelope, **changes})
