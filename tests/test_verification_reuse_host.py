# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A reused check keeps its charged execution and original observation time."""

import pytest


def test_reused_check_keeps_original_provenance_and_rejects_failed_or_unknown_results():
    from firefly_forge.verification_result_reuse import retained_check

    record = dict(digest="1" * 64, created_at="2026-10-01T00:00:00Z", payload=dict(execution_id="2" * 32))
    value = dict(effects=dict(start=dict(status="done", evidence=dict(exit_code=0, output="prior output"))))
    actual = retained_check(["python", "-m", "ruff"], 2, 2, record, value)
    assert actual == dict(
        argv=["python", "-m", "ruff"],
        attempt=2,
        configured_check_index=2,
        exit_code=0,
        output="prior output",
        created_at=record["created_at"],
        execution="reused",
        source_execution_id="2" * 32,
        source_record_digest="1" * 64,
    )
    for exit_code in (1, None, False, 0.0):
        value["effects"]["start"]["evidence"]["exit_code"] = exit_code
        with pytest.raises(ValueError):
            retained_check(["python"], 2, 2, record, value)
    value["effects"]["start"]["status"] = "unknown"
    value["effects"]["start"]["evidence"]["exit_code"] = 0
    with pytest.raises(ValueError):
        retained_check(["python"], 2, 2, record, value)
