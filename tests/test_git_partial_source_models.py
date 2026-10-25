# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A partial source checkpoint is explicit source custody, never execution consent."""

import pytest


def consent():
    return dict(
        version=1,
        candidate_digest="a" * 64,
        policy_digest="b" * 64,
        expected_run_digest="c" * 64,
        journal_digest="d" * 64,
        source_digest="e" * 64,
        frontier_record_digest="f" * 64,
        acknowledge_partial_unverified=True,
        acknowledge_preserve_failed_graph=True,
        acknowledge_no_execution_grant=True,
    )


def test_partial_consent_is_exclusive_and_legacy_requests_do_not_change():
    from firefly_forge.git_delivery_models import GitDeliveryInputV1

    old = dict(version=1, request_id="1" * 32, expected_head="2" * 40, expected_source_tree="3" * 40)
    assert GitDeliveryInputV1.model_validate(old).model_dump(mode="json") == old
    new = dict(
        version=1,
        request_id="1" * 32,
        expected_head="2" * 40,
        expected_source_digest="e" * 64,
        explicit_partial_source=consent(),
    )
    assert GitDeliveryInputV1.model_validate(new).model_dump(mode="json") == new
    with pytest.raises(ValueError):
        GitDeliveryInputV1.model_validate({**new, "explicit_partial_source": {**consent(), "execute": True}})


@pytest.mark.parametrize("value", [False, 1, "true", None])
@pytest.mark.parametrize(
    "field", ["acknowledge_partial_unverified", "acknowledge_preserve_failed_graph", "acknowledge_no_execution_grant"]
)
def test_partial_consent_requires_exact_acknowledgments(field, value):
    from firefly_forge.git_partial_source_models import GitPartialSourceConsentV1

    with pytest.raises(ValueError):
        GitPartialSourceConsentV1.model_validate({**consent(), field: value})
