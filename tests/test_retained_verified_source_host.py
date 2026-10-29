# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Strict retained-source authority cannot be selected with an unbound boolean or stale digest."""

from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from firefly_forge.final_outcome_models import RetainedVerifiedSourceV1
from firefly_forge.identity import Principal
from firefly_forge.journal import digest
from firefly_forge.project_access import CURRENT_PRINCIPAL
from firefly_forge.retained_verified_source import prepare_retained_source


def proof():
    value = dict(
        version=1,
        kind="retained_verified_final_source",
        run_id="a" * 32,
        request_id="b" * 32,
        attempt=3,
        parent_attempt=2,
        task_key="c" * 32,
        **{
            key: "1" * 64
            for key in (
                "final_outcome_witness_digest",
                "terminal_receipt_digest",
                "verification_custody_digest",
                "handoff_intent_digest",
                "handoff_outcome_digest",
                "continuation_intent_digest",
                "source_digest",
                "commands_digest",
                "sandbox_digest",
                "scope_digest",
            )
        },
    )
    return {**value, "digest": digest(value)}


def test_retained_verified_source_requires_exact_typed_bound_proof():
    value = proof()
    assert RetainedVerifiedSourceV1.model_validate(value).model_dump() == value
    for patch in (
        {"version": True},
        {"attempt": True},
        {"attempt": 4},
        {"parent_attempt": "2"},
        {"extra": True},
        {"source_digest": "2" * 64},
        {"request_id": "foreign"},
    ):
        with pytest.raises(ValidationError):
            RetainedVerifiedSourceV1.model_validate({**value, **patch})


@pytest.mark.parametrize("marker", [None, {}, {"request_id": "a" * 32}])
async def test_no_unbound_retained_source_authority_or_io(marker, tmp_path):
    async def owned():
        pass

    store = SimpleNamespace(execution_ownership_token=object(), assert_ownership=owned)
    token = CURRENT_PRINCIPAL.set(Principal(UUID("a" * 32), "https://identity.invalid", "fictional-operator"))
    try:
        with pytest.raises(ValueError, match="accepted native"):
            await prepare_retained_source(store, None, {"id": "a" * 32, "invalid_read_continuity": marker}, tmp_path)
    finally:
        CURRENT_PRINCIPAL.reset(token)
    assert list(tmp_path.iterdir()) == []


async def test_lost_retained_source_owner_is_refused_before_native_or_source_io(tmp_path):
    async def lost():
        raise ValueError("Controlled revoked worker ownership")

    store = SimpleNamespace(assert_ownership=lost)
    with pytest.raises(ValueError, match="revoked worker"):
        await prepare_retained_source(store, None, {"id": "a" * 32}, tmp_path)
    assert list(tmp_path.iterdir()) == []
