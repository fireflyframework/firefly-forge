# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Constructed b686 wire records; no native Bridge execution or cross-runtime acceptance."""

import copy
import hashlib
import json

import pytest

from firefly_forge.bridge_completion_models import ObservationInput, validate_remote_snapshot


def seal(value):
    value = {key: item for key, item in value.items() if key != "digest"}
    return {
        **value,
        "digest": hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
    }


def snapshot():
    descriptor = {
        "request_id": "fbr1_" + "a" * 32,
        "principal_id": "fixture",
        "model": "bridge/default",
        "wire_sha256": "b" * 64,
        "wire_bytes": 100,
        "image_revision": None,
    }
    job = "00000000-0000-4000-8000-000000000001"
    admission = seal(
        {
            "version": 1,
            "request_id": descriptor["request_id"],
            "principal_id": "fixture",
            "job_id": job,
            "model": "bridge/default",
            "fingerprint": "c" * 64,
            "fingerprint_algorithm": "sha256-json-stringify-prepared-v1",
            "wire_sha256": descriptor["wire_sha256"],
            "wire_bytes": 100,
            "image_revision": None,
            "created_at": "2026-10-16T01:00:00.000Z",
        }
    )
    result = json.dumps(
        {
            "id": "chatcmpl-" + job,
            "object": "chat.completion",
            "created": 1,
            "model": "bridge/default",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "Original Ω result"}, "finish_reason": "stop"}
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    terminal = seal(
        {
            "version": 1,
            "job_id": job,
            "admission_digest": admission["digest"],
            "attempt_id": None,
            "status": "succeeded",
            "recorded_at": "2026-10-16T01:01:00.000Z",
            "result_json": result,
            "result_sha256": hashlib.sha256(result.encode()).hexdigest(),
            "result_bytes": len(result.encode()),
        }
    )
    return descriptor, {
        "version": 1,
        "admission": admission,
        "state": "terminal",
        "terminal": terminal,
        "current_job_status": "running",
        "prepared_at": "2026-10-16T01:02:00.000Z",
        "observations": None,
    }


def test_original_terminal_is_valid_even_if_later_job_is_running():
    descriptor, value = snapshot()
    original = copy.deepcopy(value)
    assert validate_remote_snapshot(value, descriptor) == original
    assert value["terminal"]["status"] == "succeeded" and value["current_job_status"] == "running"
    assert json.loads(value["terminal"]["result_json"])["choices"][0]["message"]["content"] == "Original Ω result"


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "fbr1_" + "e" * 32),
        ("principal_id", "another"),
        ("model", "bridge/other"),
        ("wire_bytes", 101),
        ("wire_sha256", "e" * 64),
        ("image_revision", "e" * 64),
    ],
)
def test_even_resigned_admission_cannot_swap_original_request_scope(field, value):
    descriptor, record = snapshot()
    record["admission"][field] = value
    record["admission"] = seal(record["admission"])
    with pytest.raises(ValueError):
        validate_remote_snapshot(record, descriptor)


@pytest.mark.parametrize(
    "mutation",
    [
        "version",
        "extra",
        "digest",
        "result",
        "result_bytes",
        "result_hash",
        "job",
        "admission",
        "status",
        "state",
        "observations",
    ],
)
def test_malformed_terminal_or_result_is_refused_without_repair(mutation):
    descriptor, value = snapshot()
    if mutation == "version":
        value["terminal"]["version"] = True
    elif mutation == "extra":
        value["terminal"]["extra"] = "unexpected"
    elif mutation == "digest":
        value["terminal"]["digest"] = "e" * 64
    elif mutation == "result":
        value["terminal"]["result_json"] += "changed"
    elif mutation == "result_bytes":
        value["terminal"]["result_bytes"] += 1
    elif mutation == "result_hash":
        value["terminal"]["result_sha256"] = "e" * 64
    elif mutation == "job":
        value["terminal"]["job_id"] = "00000000-0000-4000-8000-000000000002"
    elif mutation == "admission":
        value["terminal"]["admission_digest"] = "e" * 64
    elif mutation == "status":
        value["terminal"]["status"] = "failed"
    elif mutation == "state":
        value["state"] = "pending"
    else:
        value["observations"] = {"reason": "unknown", "retry_at": None}
    with pytest.raises(ValueError):
        validate_remote_snapshot(value, descriptor)


def test_observation_identity_is_strict_and_does_not_mint_a_new_request():
    with pytest.raises(ValueError):
        ObservationInput.model_validate(
            {"version": True, "observation_id": "a" * 32, "expected_intent_digest": "b" * 64}
        )
    with pytest.raises(ValueError):
        ObservationInput.model_validate({"observation_id": "a" * 32, "expected_intent_digest": "b" * 64, "retry": True})
