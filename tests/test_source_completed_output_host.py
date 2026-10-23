# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A completed model turn is observed output, never successful implementation or an exit attestation."""

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from firefly_forge.activity_models import project_activity
from firefly_forge.source_result_readmission import completed_output_observation


def output_fixture():
    run_id = uuid4().hex
    now = datetime.now(UTC)
    payloads = [
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "id": "final",
                "type": "agent_message",
                "text": '{"summary":"Reader failed","feedback":{"disposition":"blocked","request":null}}',
            },
        },
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}},
    ]
    events, contents = [], {}
    for offset, payload in enumerate(payloads, 1):
        event_id = uuid4().hex
        message = json.dumps(payload, separators=(",", ":"))
        projection, raw = project_activity(run_id, event_id, "stdout", message, retain=True)
        events.append(
            {
                "id": event_id,
                "stage": "stdout",
                "created_at": (now + timedelta(seconds=offset)).isoformat(),
                **projection,
            }
        )
        contents[event_id] = raw
    run = {"id": run_id, "summary": payloads[1]["item"]["text"], "events": events}
    dispatch = {"digest": "a" * 64, "created_at": now.isoformat()}
    return run, dispatch, contents


def test_completed_turn_binds_full_archived_output_and_never_attests_exit_or_success():
    run, dispatch, contents = output_fixture()
    result = completed_output_observation(run, dispatch, contents)
    assert result["dispatch_digest"] == dispatch["digest"]
    assert result["summary_sha256"] == hashlib.sha256(run["summary"].encode()).hexdigest()
    assert len(result["events"]) == 3
    assert result["implementation_success"] is False
    assert result["process_exit_attested"] is False
    assert "diagnostics" not in result
    assert "Reader failed" not in json.dumps(result)


def test_optional_native_thread_header_precedes_the_single_started_turn():
    run, dispatch, contents = output_fixture()
    event_id = uuid4().hex
    projection, raw = project_activity(
        run["id"], event_id, "stdout", '{"type":"thread.started","thread_id":"public-fixture"}', retain=True
    )
    run["events"].insert(
        0,
        {
            "id": event_id,
            "stage": "stdout",
            "created_at": (datetime.fromisoformat(dispatch["created_at"]) + timedelta(milliseconds=100)).isoformat(),
            **projection,
        },
    )
    contents[event_id] = raw
    assert len(completed_output_observation(run, dispatch, contents)["events"]) == 4


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("valid_for_seconds", True),
        ("valid_for_seconds", 1801),
        ("acknowledge_output_not_success", 1),
        ("acknowledge_no_exit_attestation", False),
        ("unexpected", "value"),
    ],
)
def test_completed_output_admission_requires_exact_bounded_explicit_consent(field, value):
    from firefly_forge.source_result_readmission import SourceCompletedOutputInput

    body = {
        "version": 1,
        "request_id": uuid4().hex,
        "candidate_digest": "a" * 64,
        "admission_contract_digest": "b" * 64,
        "previous_catalog_path": "/public/fixture/catalog.json",
        "valid_for_seconds": 600,
        "acknowledge_output_not_success": True,
        "acknowledge_no_exit_attestation": True,
        "acknowledge_original_budgets": True,
        "acknowledge_no_replay": True,
        "acknowledge_new_finite_attempt": True,
    }
    body[field] = value
    with pytest.raises(ValueError):
        SourceCompletedOutputInput.model_validate(body)


@pytest.mark.parametrize(
    "change",
    [
        "missing_content",
        "content_drift",
        "summary_drift",
        "wrong_run",
        "before_dispatch",
        "no_complete",
        "duplicate_start",
        "later_message",
        "failed_turn",
    ],
)
def test_unproven_or_ambiguous_turn_never_authorizes_another_dispatch(change):
    run, dispatch, contents = output_fixture()
    if change == "missing_content":
        contents.pop(run["events"][1]["id"])
    elif change == "content_drift":
        contents[run["events"][1]["id"]] += b" "
    elif change == "summary_drift":
        run["summary"] = "Different output"
    elif change == "wrong_run":
        run["id"] = uuid4().hex
    elif change == "before_dispatch":
        dispatch["created_at"] = run["events"][-1]["created_at"]
    elif change == "no_complete":
        run["events"].pop()
    elif change == "duplicate_start":
        run["events"].insert(1, copy.deepcopy(run["events"][0]))
    elif change == "later_message":
        run["events"].append(copy.deepcopy(run["events"][1]))
    else:
        event = run["events"][-1]
        projection, raw = project_activity(run["id"], event["id"], "stdout", '{"type":"turn.failed"}', retain=True)
        event.update(projection)
        contents[event["id"]] = raw
    with pytest.raises(ValueError):
        completed_output_observation(run, dispatch, contents)


def notice_fixture():
    run, dispatch, _ = output_fixture()
    run["agent"] = "codex"
    message = (
        "Under-development features enabled: skip_host_skill_discovery. "
        "Under-development features are incomplete and may behave unpredictably. "
        "To suppress this warning, set `suppress_unstable_features_warning = true` "
        "in /public/fixture/codex-home/config.toml."
    )
    payloads = [
        {"type": "thread.started", "thread_id": "public-fixture"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "error", "message": message}},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "interim", "type": "agent_message", "text": "Reading retained source"},
        },
        {"type": "item.started", "item": {"id": "reader", "type": "mcp_tool_call"}},
        {"type": "item.completed", "item": {"id": "reader", "type": "mcp_tool_call"}},
        {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": run["summary"]}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}},
    ]
    return run, dispatch, payloads


def project_notice_events(run, dispatch, payloads):
    events, contents = [], {}
    boundary = datetime.fromisoformat(dispatch["created_at"])
    for index, payload in enumerate(payloads, 1):
        event_id = uuid4().hex
        projection, raw = project_activity(run["id"], event_id, "stdout", json.dumps(payload), retain=True)
        events.append(
            {
                "id": event_id,
                "stage": "stdout",
                "created_at": (boundary + timedelta(milliseconds=index)).isoformat(),
                **projection,
            }
        )
        contents[event_id] = raw
    run["events"] = events
    return contents


def test_exact_native_pre_turn_notice_retains_all_eight_frames_without_attesting_success():
    run, dispatch, payloads = notice_fixture()
    contents = project_notice_events(run, dispatch, payloads)
    result = completed_output_observation(run, dispatch, contents)
    assert len(result["events"]) == 8
    assert result["diagnostics"] == [
        {
            "kind": "codex_unstable_feature_notice_v1",
            "feature": "skip_host_skill_discovery",
            "event_id": run["events"][1]["id"],
            "message_sha256": hashlib.sha256(payloads[1]["item"]["message"].encode()).hexdigest(),
            "frame_sha256": hashlib.sha256(contents[run["events"][1]["id"]]).hexdigest(),
        }
    ]
    assert result["implementation_success"] is False and result["process_exit_attested"] is False
    assert "/public/fixture" not in json.dumps(result)


@pytest.mark.parametrize(
    "change",
    [
        "other_feature",
        "other_error",
        "duplicate",
        "late",
        "effect_before",
        "extra_field",
        "relative_path",
        "parent_path",
        "newline",
        "wrong_provider",
        "wrong_item",
        "unknown_outer_error",
        "summary",
        "failed_turn",
    ],
)
def test_pre_turn_notice_does_not_admit_unknown_errors_effects_or_unbound_output(change):
    run, dispatch, payloads = notice_fixture()
    notice = payloads[1]
    if change == "other_feature":
        notice["item"]["message"] = notice["item"]["message"].replace("skip_host_skill_discovery", "unknown_feature")
    elif change == "other_error":
        notice["item"]["message"] = "An actual provider error occurred"
    elif change == "duplicate":
        payloads.insert(2, copy.deepcopy(notice))
    elif change == "late":
        payloads.insert(4, payloads.pop(1))
    elif change == "effect_before":
        payloads.insert(2, {"type": "item.started", "item": {"id": "command", "type": "command_execution"}})
    elif change == "extra_field":
        notice["item"]["command"] = "not a notice"
    elif change == "relative_path":
        notice["item"]["message"] = notice["item"]["message"].replace("/public/fixture/codex-home/", "")
    elif change == "parent_path":
        notice["item"]["message"] = notice["item"]["message"].replace("/public/fixture/", "/public/../fixture/")
    elif change == "newline":
        notice["item"]["message"] += "\nextra"
    elif change == "wrong_provider":
        run["agent"] = "agentic"
    elif change == "wrong_item":
        notice["item"]["type"] = "command_execution"
    elif change == "unknown_outer_error":
        notice["type"] = "error"
    elif change == "summary":
        run["summary"] = "Different final output"
    else:
        payloads[-1]["type"] = "turn.failed"
    contents = project_notice_events(run, dispatch, payloads)
    with pytest.raises(ValueError):
        completed_output_observation(run, dispatch, contents)
