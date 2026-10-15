# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""V2 attempts cannot alias a legacy UUID-only observation request."""

import importlib.util

import pytest
from pydantic import ValidationError

from firefly_forge.learning_controllers import LearningObservation


def packet():
    return {
        "attempt_id": "obs2_" + "a" * 32,
        "source_id": "owned-source",
        "projection_id": "b" * 32,
        "submission_request_id": "c" * 32,
        "workspace_id": "forge-learning-owned",
        "target_revision": "d" * 32,
        "payload_digest": "e" * 64,
        "content_sha256": "f" * 64,
    }


def model():
    if importlib.util.find_spec("firefly_forge.learning_observation_attempt_models") is None:
        return LearningObservation
    from firefly_forge.learning_observation_attempt_models import LearningObservationAttemptInput

    return LearningObservationAttemptInput


def test_new_namespace_has_a_strict_admitted_request_shape():
    body = model().model_validate(packet())
    assert body.model_dump() == packet()
    with pytest.raises(ValidationError):
        LearningObservation(request_id=packet()["attempt_id"], source_id="owned-source")


@pytest.mark.parametrize(
    ("field", "value"),
    [("attempt_id", "a" * 32), ("source_id", True), ("projection_id", True), ("content_sha256", "bad")],
)
def test_attempt_rejects_legacy_ids_and_untyped_or_invalid_fields(field, value):
    with pytest.raises(ValidationError):
        model().model_validate({**packet(), field: value})


def test_attempt_does_not_accept_caller_disposition_or_completion():
    with pytest.raises(ValidationError):
        model().model_validate({**packet(), "state": "not_dispatched"})


@pytest.mark.parametrize(("state", "finished"), [("preparing", "2026-10-15T00:00:00Z"), ("unknown", None)])
def test_receipt_requires_timestamps_consistent_with_its_retained_phase(state, finished):
    from firefly_forge.learning_observation_attempt_models import LearningObservationAttemptReceipt

    base = {key: value for key, value in packet().items() if key != "source_id"}
    with pytest.raises(ValidationError):
        LearningObservationAttemptReceipt.model_validate(
            {
                **base,
                "version": 2,
                "record_id": "1" * 32,
                "repository_id": "2" * 32,
                "project_id": None,
                "run_id": "3" * 32,
                "actor_id": "4" * 32,
                "input_digest": "5" * 64,
                "source_digest": "6" * 64,
                "state": state,
                "disposition": None,
                "created_at": "2026-10-15T00:00:00Z",
                "finished_at": finished,
                "observation": None,
                "digest": "7" * 64,
            }
        )


def test_python_canonical_digest_matches_shared_ascii_and_null_contract_vectors():
    from firefly_forge.journal import digest

    assert (
        digest(model().model_validate(packet()).model_dump())
        == "26d27b2ea636c7f2fc608861df338fc280cbcf6c1d6feddf0bcc19cc7ea3e263"
    )
    assert digest({"source_id": "owned-source"}) == "569c2816bc705d38b9cb571e9d3482ed3ddec5420ccb3a958731ba914f9652e6"
    assert (
        digest(model().model_validate({**packet(), "source_id": None}).model_dump())
        == "a3d54e0f2fcb0154a76804201ee8602adffe1ba2d2dfd6139f06dfbd57e2cee3"
    )
