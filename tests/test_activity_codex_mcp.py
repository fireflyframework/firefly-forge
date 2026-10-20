# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Actual finite-client messages and controlled malformed display vectors, without live execution."""

import json
from pathlib import Path

import pytest

from firefly_forge.activity_models import (
    MAX_EVENT_BYTES,
    ActivityContentQuery,
    canonical,
    content_chunk,
    project_activity,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures/activity-codex-mcp-v1.json").read_text())


def project(event):
    message = json.dumps(event, ensure_ascii=False)
    result, payload = project_activity(FIXTURE["run_id"], "a" * 32, "stdout", message)
    assert payload == message.encode()
    assert len(canonical(result)) <= 4096
    return result


def test_actual_finite_codex_mcp_messages_keep_original_bytes_and_observed_tool_metadata():
    for record in FIXTURE["records"]:
        event = record["event"]
        data, payload = project_activity(FIXTURE["run_id"], event["id"], "stdout", record["raw_message"])
        assert payload == record["raw_message"].encode()
        assert data == {key: value for key, value in event.items() if key not in {"id", "stage"}}
        summary = data["activity_summary"]
        assert summary["tool_server"] == "forge_coding" and summary["tool_name"] == "run_configured_check"
        assert "arguments" not in summary and "sandbox_execution_ids" not in summary
        assert "exit_code" not in summary
        assert (
            content_chunk(
                data["activity_content"],
                payload,
                ActivityContentQuery(expected_descriptor=data["activity_content"]["descriptor_sha256"]),
            )
            == record["chunk"]
        )


@pytest.mark.parametrize("status", ["in_progress", "completed", "failed"])
def test_reported_mcp_status_never_infers_a_native_receipt_or_exit(status):
    event = json.loads(FIXTURE["records"][0]["raw_message"])
    event["item"]["status"] = status
    event["type"] = "item.completed" if status != "in_progress" else "item.started"
    event["item"]["error"] = {"message": "Provider reported a failure"} if status == "failed" else None
    summary = project(event)["activity_summary"]
    assert summary["tool_status"] == status
    assert "tool_exit_code" not in summary and "exit_code" not in summary
    if status == "failed":
        assert summary["output_preview"] == "Provider reported a failure"


@pytest.mark.parametrize(
    "field,value",
    [
        ("server", []),
        ("tool", "<script>"),
        ("status", True),
        ("status", "unknown"),
        ("arguments", []),
        ("result", "malformed"),
        ("error", False),
    ],
)
def test_malformed_mcp_metadata_uses_the_existing_unknown_fallback(field, value):
    event = json.loads(FIXTURE["records"][0]["raw_message"])
    event["item"][field] = value
    assert "activity_summary" not in project(event)


def test_known_result_output_is_bounded_but_unsupported_objects_remain_raw_only():
    event = json.loads(FIXTURE["records"][1]["raw_message"])
    event["item"]["result"] = {"structured_content": {"output": "🙂\x00\n" * 10000, "exit_code": True}}
    summary = project(event)["activity_summary"]
    assert len(summary["output_preview"].encode()) <= 768
    assert "tool_exit_code" not in summary
    event["item"]["result"] = {"content": [{"type": "image", "data": "RAW_ONLY"}]}
    summary = project(event)["activity_summary"]
    assert "output_preview" not in summary and "RAW_ONLY" not in json.dumps(summary)


def test_plain_mcp_text_and_structured_text_have_distinct_safe_previews():
    event = json.loads(FIXTURE["records"][1]["raw_message"])
    event["item"]["result"] = {"content": [{"type": "text", "text": "Tool response text"}]}
    assert project(event)["activity_summary"]["output_preview"] == "Tool response text"
    event["item"]["result"]["content"][0]["text"] = '{"unsupported":"RAW_ONLY"}'
    summary = project(event)["activity_summary"]
    assert "output_preview" not in summary


def test_mcp_summary_obeys_existing_oversized_and_duplicate_key_refusals():
    event = json.loads(FIXTURE["records"][1]["raw_message"])
    event["item"]["result"] = {"structured_content": {"output": "x" * MAX_EVENT_BYTES}}
    message = json.dumps(event)
    data, payload = project_activity(FIXTURE["run_id"], "a" * 32, "stdout", message)
    assert "activity_summary" not in data and payload is None
    message = FIXTURE["records"][0]["raw_message"].replace(
        '"server":"forge_coding"', '"server":"other","server":"forge_coding"'
    )
    data, _ = project_activity(FIXTURE["run_id"], "a" * 32, "stdout", message)
    assert "activity_summary" not in data


def test_maximum_observed_identities_and_escaped_output_fit_the_existing_index():
    event = json.loads(FIXTURE["records"][1]["raw_message"])
    event["item"].update(id="i" * 128, server="s" * 64, tool="t" * 64)
    event["item"]["result"] = {"structured_content": {"output": '\\"\n\x00🙂' * 10000, "exit_code": -2}}
    summary = project(event)["activity_summary"]
    assert summary["tool_exit_code"] == -2
    assert len(summary["output_preview"].encode()) <= 768


def test_documentation_uses_the_same_actual_redacted_message_vectors():
    guide = Path(__file__).parents[1] / "studio/tests/provider-mcp-activity-guide-fixture.json"
    assert json.loads(guide.read_text()) == FIXTURE
