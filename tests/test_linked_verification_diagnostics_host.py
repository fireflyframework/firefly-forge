# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Safe typed hold provenance; never arbitrary exception text or changed progress."""

import pytest
from linked_verification_fixtures import receipt
from pydantic import ValidationError

from firefly_forge.linked_verification_models import LinkedVerificationReceiptV1


def diagnostic_receipt():
    value = receipt(status="needs_reconciliation")
    value["progress"].update(started=1, completed=1, unknown=False)
    value["hold_diagnostic"] = {
        "phase": "post_result_source",
        "cause_code": "selected_source_changed",
        "exception_type": "ValueError",
    }
    return value


def test_known_result_diagnostic_does_not_invent_unknown_or_failed_command():
    result = LinkedVerificationReceiptV1.model_validate(diagnostic_receipt()).model_dump(mode="json")
    assert result["progress"]["unknown"] is False and result["progress"]["failed"] is False
    assert result["hold_diagnostic"]["phase"] == "post_result_source"
    assert "last_failure" not in result


@pytest.mark.parametrize(
    "field,value",
    [
        ("phase", "raw secret"),
        ("cause_code", "raw secret"),
        ("exception_type", "sensitive-custom-type"),
        ("message", "raw secret"),
    ],
)
def test_diagnostic_refuses_unbounded_or_raw_exception_data(field, value):
    body = diagnostic_receipt()
    body["hold_diagnostic"][field] = value
    with pytest.raises(ValidationError):
        LinkedVerificationReceiptV1.model_validate(body)


@pytest.mark.parametrize("status", ["queued", "executing", "completed"])
def test_diagnostic_requires_an_actual_closed_hold(status):
    body = receipt(status=status)
    body["hold_diagnostic"] = diagnostic_receipt()["hold_diagnostic"]
    with pytest.raises(ValidationError):
        LinkedVerificationReceiptV1.model_validate(body)
