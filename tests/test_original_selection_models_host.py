# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit consent cannot be inferred from truthy input."""

import importlib.util

import pytest


def test_native_original_selection_contract_is_available():
    assert importlib.util.find_spec("firefly_forge.original_selection_models") is not None


def test_native_original_selection_service_is_available():
    assert importlib.util.find_spec("firefly_forge.original_selection_readmissions") is not None


def test_original_selection_events_fit_the_native_journal_column():
    from firefly_forge.original_selection_runtime import CLAIM, INTENT, READMISSION
    from firefly_forge.persistence import ForgeJournalEntity

    width = ForgeJournalEntity.__table__.c.kind.type.length
    assert all(len(kind) <= width for kind in (CLAIM, INTENT, READMISSION))


@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_original_selection_consent_is_literal_native_operator_consent(value):
    from pydantic import ValidationError

    from firefly_forge.original_selection_models import OriginalSelectionReadmissionInput

    fields = {
        "version": 1,
        "candidate_digest": "a" * 64,
        "policy_digest": "b" * 64,
        "operator_observation_digest": "c" * 64,
        "observation_provenance": "native_operator_pre_dispatch_observation",
        "acknowledge_known_local_readiness_failure": True,
        "acknowledge_no_external_model_dispatch": True,
        "acknowledge_original_selection_only": True,
        "acknowledge_original_mode_and_claim_preserved": True,
        "acknowledge_spent_history_and_budgets_preserved": True,
        "acknowledge_same_native_continuation": True,
        "acknowledge_existing_unknown_command_risk": True,
    }
    assert OriginalSelectionReadmissionInput.model_validate(fields).model_dump() == fields
    fields["acknowledge_original_selection_only"] = value
    with pytest.raises(ValidationError):
        OriginalSelectionReadmissionInput.model_validate(fields)
