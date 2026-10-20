# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Full sanitized messages and small observed activity projections are separate."""

import base64
import hashlib
import importlib
import json

import pytest

RUN, EVENT = "a" * 32, "b" * 32


def module():
    return importlib.import_module("firefly_forge.activity_models")


def command_message(output):
    return json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "item-4",
                "type": "command_execution",
                "command": "cat public-fixture.txt",
                "aggregated_output": output,
                "exit_code": 0,
                "status": "completed",
            },
        },
        ensure_ascii=False,
    )


@pytest.mark.parametrize("size", [9000, 70000, 1048576])
def test_large_command_keeps_exit_code_and_full_sanitized_archive_outside_index(size):
    m = module()
    message = command_message("X" * size)
    data, payload = m.project_activity(RUN, EVENT, "stdout", message)
    assert payload == message.encode()
    assert len(m.canonical(data)) <= 4096
    assert len(data["message"].encode()) <= 1024
    summary = data["activity_summary"]
    assert summary["version"] == 1 and summary["exit_code"] == 0
    assert summary["event_type"] == "item.completed" and summary["item_type"] == "command_execution"
    assert summary["command_preview"] == "cat public-fixture.txt"
    assert summary["preview_omitted"] is True
    descriptor = data["activity_content"]
    assert descriptor["status"] == "available"
    assert descriptor["sanitized_message_sha256"] == hashlib.sha256(payload).hexdigest()
    assert descriptor["sanitized_message_bytes"] == len(payload)
    assert summary["sanitized_message_sha256"] == descriptor["sanitized_message_sha256"]


def test_chunk_reconstruction_preserves_unicode_and_complete_sanitized_hash():
    m = module()
    data, payload = m.project_activity(RUN, EVENT, "stderr", "a" * 16383 + "🙂" * 10000)
    descriptor = data["activity_content"]
    chunks = []
    ordinal = 0
    while True:
        query = m.ActivityContentQuery(expected_descriptor=descriptor["descriptor_sha256"], chunk=ordinal)
        response = m.content_chunk(descriptor, payload, query)
        assert len(m.canonical(response)) <= 32768
        chunks.append(base64.b64decode(response["data_base64"], validate=True))
        assert response["offset"] == ordinal * 16384
        assert response["total_bytes"] == len(payload)
        assert response["sanitized_message_sha256"] == hashlib.sha256(payload).hexdigest()
        if response["next_chunk"] is None:
            break
        ordinal = response["next_chunk"]
    assert b"".join(chunks) == payload


@pytest.mark.parametrize("message", ["plain provider output", '{"type":"item.completed",', '{"type":"a","type":"b"}'])
def test_unrecognized_or_malformed_output_remains_text_without_invented_command_metadata(message):
    data, payload = module().project_activity(RUN, EVENT, "stdout", message)
    assert "activity_summary" not in data
    assert payload == message.encode() and data["message"] == message


@pytest.mark.parametrize("reason", ["event_size", "run_budget"])
def test_budget_refusal_never_claims_a_complete_retained_payload(reason):
    m = module()
    message = "x" * (m.MAX_EVENT_BYTES + 1) if reason == "event_size" else "known output"
    data, payload = m.project_activity(RUN, EVENT, "stdout", message, retain=reason != "run_budget")
    assert payload is None
    descriptor = data["activity_content"]
    assert descriptor["status"] == "budget_exceeded"
    with pytest.raises(m.ActivityUnavailable, match="activity_budget_exceeded"):
        m.content_chunk(descriptor, b"", m.ActivityContentQuery(expected_descriptor=descriptor["descriptor_sha256"]))


@pytest.mark.parametrize("mutation", ["payload", "descriptor", "expected", "chunk", "version"])
def test_content_chunks_fail_closed_on_tampering_or_invalid_selection(mutation):
    m = module()
    data, payload = m.project_activity(RUN, EVENT, "stdout", "whole output")
    descriptor = data["activity_content"]
    query = dict(expected_descriptor=descriptor["descriptor_sha256"], chunk=0)
    if mutation == "payload":
        payload = b"changed"
    elif mutation == "descriptor":
        descriptor = {**descriptor, "event_id": "c" * 32}
    elif mutation == "expected":
        query["expected_descriptor"] = "0" * 64
    elif mutation == "chunk":
        query["chunk"] = 1
    else:
        descriptor = {**descriptor, "version": 2}
    with pytest.raises((ValueError, m.ActivityUnavailable)):
        m.content_chunk(descriptor, payload, m.ActivityContentQuery(**query))


def test_new_archive_index_does_not_inflate_the_existing_evidence_snapshot():
    from test_run_evidence import sample

    from firefly_forge.run_evidence import MAX_BYTES, build_evidence, encoded

    m = module()
    fixture = sample()
    data, _ = m.project_activity(RUN, EVENT, "stdout", command_message("x" * 1048576))
    fixture["activity"] = [{"id": EVENT, "sequence": 1, "data": {"id": EVENT, "stage": "stdout", **data}}]
    snapshot = build_evidence(**fixture)
    assert MAX_BYTES == 16777216
    assert len(encoded(snapshot.members["activity.json"])) < 5000
    assert snapshot.members["activity.json"][0]["data"]["activity_content"]["status"] == "available"
    assert snapshot.manifest["activity_content_bytes"] == "not_included_with_bound_references"


@pytest.mark.parametrize("message", ['{"type":[]}', '{"type":"item.completed","item":{"type":[]}}'])
def test_untrusted_non_string_event_discriminators_are_text_not_parser_failures(message):
    data, payload = module().project_activity(RUN, EVENT, "stdout", message)
    assert "activity_summary" not in data and payload == message.encode()


def test_json_escaped_previews_respect_the_total_index_budget():
    m = module()
    data, payload = m.project_activity(RUN, EVENT, "stderr", "\x00" * 1024)
    assert payload == b"\x00" * 1024
    assert len(m.canonical({"id": EVENT, "stage": "stderr", "created_at": "2026-10-20T00:00:00+00:00", **data})) <= 4096


def test_shared_python_typescript_fixture_is_produced_by_the_native_contract():
    from pathlib import Path

    fixture = json.loads((Path(__file__).parent / "fixtures/activity-archive-contract-v1.json").read_text())
    event = fixture["event"]
    data, payload = module().project_activity(
        event["activity_content"]["run_id"], event["id"], event["stage"], fixture["sanitized_message"]
    )
    assert data["activity_content"] == event["activity_content"]
    assert data["activity_summary"] == event["activity_summary"]
    for response in fixture["chunks"]:
        query = module().ActivityContentQuery(
            expected_descriptor=event["activity_content"]["descriptor_sha256"], chunk=response["chunk"]
        )
        assert module().content_chunk(data["activity_content"], payload, query) == response
