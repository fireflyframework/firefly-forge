# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Recovery grants no new execution ticket or consumption; wire inputs are exact."""

import pytest


def body():
    return dict(
        version=1,
        candidate_digest="1" * 64,
        expected_admission_seal="2" * 64,
        expected_failed_event_digest="3" * 64,
        valid_for_seconds=180,
        acknowledge_never_claimed=True,
        acknowledge_same_request_and_attempt=True,
        acknowledge_preserved_consumption_and_budgets=True,
        acknowledge_renewed_same_attempt_lease=True,
    )


def test_recovery_body_requires_exact_literal_acknowledgements_and_bounded_window():
    from firefly_forge.invalid_read_recovery_models import InvalidReadRecoveryInputV1

    accepted = InvalidReadRecoveryInputV1.model_validate(body())
    assert accepted.model_dump() == body()
    for key in [name for name in body() if name.startswith("acknowledge_")]:
        for value in (False, 1, "true", None):
            with pytest.raises(ValueError):
                InvalidReadRecoveryInputV1.model_validate({**body(), key: value})
    for key, value in [
        ("version", True),
        ("version", 2),
        ("valid_for_seconds", True),
        ("valid_for_seconds", 0),
        ("valid_for_seconds", 7201),
        ("valid_for_seconds", "180"),
        ("extra", "no"),
    ]:
        with pytest.raises(ValueError):
            InvalidReadRecoveryInputV1.model_validate({**body(), key: value})
