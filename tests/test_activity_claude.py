# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Public stream-json vectors; no subscription or live-provider acceptance claim."""

import hashlib
import json

import pytest

from firefly_forge.activity_models import canonical, project_activity

RUN, EVENT = "a" * 32, "b" * 32


def project(event):
    message = json.dumps(event, ensure_ascii=False)
    data, payload = project_activity(RUN, EVENT, "stdout", message)
    assert payload == message.encode()
    assert data["activity_content"]["sanitized_message_sha256"] == hashlib.sha256(payload).hexdigest()
    assert len(canonical(data)) <= 4096
    return data


def assistant(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def test_claude_assistant_text_and_tool_request_are_distinct_observations():
    text = project(assistant({"type": "text", "text": "I am reviewing the contact model."}))["activity_summary"]
    assert text["provider"] == "claude" and text["item_type"] == "agent_message"
    assert text["output_preview"] == "I am reviewing the contact model."
    request = project(
        assistant({"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "python -m pytest"}})
    )["activity_summary"]
    assert request["item_type"] == "tool_use" and request["tool_name"] == "Bash"
    assert request["command_preview"] == "python -m pytest" and request["item_id"] == "tool-1"
    assert "exit_code" not in request and "tool_outcome" not in request


@pytest.mark.parametrize("error", [False, True])
def test_claude_tool_result_reports_its_flag_without_inventing_an_exit(error):
    event = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "tool-1", "is_error": error, "content": "Observed output"}
            ]
        },
    }
    result = project(event)["activity_summary"]
    assert result["item_type"] == "tool_result" and result["item_id"] == "tool-1"
    assert result["output_preview"] == "Observed output"
    assert result["tool_outcome"] == ("reported_error" if error else "reported_result")
    assert "exit_code" not in result and "tool_name" not in result


@pytest.mark.parametrize("error,subtype", [(False, "success"), (True, "error_max_turns")])
def test_claude_result_is_a_provider_report_not_factory_acceptance(error, subtype):
    summary = project(
        {"type": "result", "subtype": subtype, "is_error": error, "result": "Provider report text", "num_turns": 3}
    )["activity_summary"]
    assert summary["event_type"] == ("claude.result_error" if error else "claude.result")
    assert summary["output_preview"] == "Provider report text" and "exit_code" not in summary


def test_mixed_blocks_keep_bounded_typed_observations_and_explicit_omissions():
    blocks = [{"type": "text", "text": "Explanation"}]
    blocks += [
        {"type": "tool_use", "id": f"tool-{n}", "name": "Read", "input": {"file_path": "PRIVATE_INPUT"}}
        for n in range(6)
    ]
    summary = project(assistant(*blocks))["activity_summary"]
    assert summary["item_type"] == "provider_message"
    assert len(summary["content_blocks"]) == 4 and summary["omitted_blocks"] == 3
    assert summary["content_blocks"][0]["item_type"] == "agent_message"
    assert summary["content_blocks"][1]["tool_name"] == "Read"
    assert "PRIVATE_INPUT" not in json.dumps(summary)
    assert summary["preview_omitted"] is True


def test_tool_result_text_blocks_preserve_order_without_serializing_attachments():
    event = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-2",
                    "content": [{"type": "text", "text": "First"}, {"type": "text", "text": "Second"}],
                }
            ]
        },
    }
    assert project(event)["activity_summary"]["output_preview"] == "First\nSecond"


@pytest.mark.parametrize(
    "event",
    [
        assistant({"type": "tool_use", "id": "tool-1", "name": 5, "input": {}}),
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "tool-1", "is_error": "false", "content": "Text"}]
            },
        },
        {"type": "result", "subtype": "success", "is_error": "false", "result": "Text"},
        {"type": "assistant", "message": {"content": "malformed"}},
    ],
)
def test_malformed_claude_shapes_remain_unclassified_without_invented_metadata(event):
    assert "activity_summary" not in project(event)


def test_claude_unicode_previews_remain_inside_existing_index_budget():
    summary = project(assistant({"type": "text", "text": "🙂" * 10000}))["activity_summary"]
    assert len(summary["output_preview"].encode()) <= 768 and summary["preview_omitted"] is True


def test_malformed_result_subtype_is_not_used_as_a_hash_key():
    assert "activity_summary" not in project({"type": "result", "subtype": [], "is_error": False, "result": "Text"})


def test_four_maximum_tool_blocks_fit_without_widening_the_index_budget():
    for names in [("Bash",) * 4, ("T" * 64,) * 4, ("Bash", "T" * 64) * 2]:
        for command in ['\\\\"\n' * 1000, "🙂" * 1000, "\x01" * 1000]:
            blocks = [
                {"type": "tool_use", "id": str(n) + "a" * 127, "name": name, "input": {"command": command}}
                for n, name in enumerate(names)
            ]
            row = project(assistant(*blocks))
            assert len(row["activity_summary"]["content_blocks"]) == 4
            assert row["activity_summary"]["omitted_blocks"] == 0


def test_public_python_typescript_vectors_preserve_the_original_descriptor_contract():
    from pathlib import Path

    vectors = json.loads((Path(__file__).parent / "fixtures/activity-claude-contract-v1.json").read_text())
    for record in vectors["records"]:
        event = record["event"]
        data, payload = project_activity(vectors["run_id"], event["id"], event["stage"], record["raw_message"])
        assert payload == record["raw_message"].encode()
        assert data == {key: value for key, value in event.items() if key not in {"id", "stage", "created_at"}}
        assert set(data["activity_content"]) == {
            "version",
            "status",
            "run_id",
            "event_id",
            "stage",
            "encoding",
            "sanitized_message_bytes",
            "sanitized_message_sha256",
            "descriptor_sha256",
        }
