# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A consumed final output can be inspected without replay or another inference."""

import copy
import importlib
import importlib.util

import pytest
from test_native_terminal_completion_host import blocked_feedback, observe_boundary

from firefly_forge.agentic_checkpoint_boundary import BoundaryCheckpoint


async def consumed_normal_result(tmp_path, monkeypatch, *, feedback=None):
    async def stop_before_receipt(env):
        env.fail_snapshot = True
        if feedback is not None:

            async def held(*args, **kwargs):
                raise ValueError("Controlled loss before accepting final feedback")

            monkeypatch.setattr(env.engine, "_worker_feedback", held)

    value = await observe_boundary(
        tmp_path, monkeypatch, False, feedback=feedback, configure=stop_before_receipt, expected_boundary=False
    )
    assert value["run"]["status"] == "failed"
    assert value["entries"][-1]["kind"] == "agent_consume"
    assert not any(e["kind"] == "receipt" and e["task_id"] == "implement" for e in value["entries"])
    return value


def implementation():
    assert importlib.util.find_spec("firefly_forge.known_final_outcome_proof"), "Missing consumed-final outcome proof"
    return importlib.import_module("firefly_forge.known_final_outcome_proof")


async def test_final_result_offline_sdk_projection_keeps_spent_usage_and_never_replays(tmp_path, monkeypatch):
    value = await consumed_normal_result(tmp_path, monkeypatch)
    before = copy.deepcopy((value["run"], value["entries"]))
    module = implementation()
    prepared = await module.prepare_final_outcome(value["run"], value["task"], value["entries"])
    data = prepared.data
    cp = data["checkpoint"]["payload"]
    captured = BoundaryCheckpoint.decode(cp["snapshot"].encode(), cp["checkpoint_digest"])
    assert data["report"] == {"summary": "Known normal terminal report", "worker_feedback": None, "qa_report": None}
    assert data["usage"]["requests"] == captured.usage.requests + 1
    assert data["usage"]["tool_calls"] == captured.usage.tool_calls == 1
    assert data["limits"] == captured.record.limits
    assert data["state"] == captured.state
    assert data["consume"] == value["entries"][-1]
    assert len(value["environment"].posts) == 2
    assert (value["environment"].workspace / "service.py").read_text() == "VALUE = 1\n"
    assert (value["run"], value["entries"]) == before


async def test_final_result_preserves_typed_blocker_and_its_requested_followup(tmp_path, monkeypatch):
    feedback = blocked_feedback()
    value = await consumed_normal_result(tmp_path, monkeypatch, feedback=feedback)
    prepared = await implementation().prepare_final_outcome(value["run"], value["task"], value["entries"])
    assert prepared.data["report"]["worker_feedback"] == value["run"]["usage"]["runtime_report"]["worker_feedback"]
    assert prepared.data["report"]["worker_feedback"]["disposition"] == "blocked"
    assert prepared.data["report"]["worker_feedback"]["request"]["kind"] == "other"
    assert len(value["environment"].posts) == 2


async def test_final_result_requires_exact_existing_consumption(tmp_path, monkeypatch):
    value = await consumed_normal_result(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="consum|frontier"):
        await implementation().prepare_final_outcome(value["run"], value["task"], value["entries"][:-1])


async def test_final_result_rejects_changed_response_custody_without_callbacks(tmp_path, monkeypatch):
    from firefly_forge.journal import digest

    value = await consumed_normal_result(tmp_path, monkeypatch)
    entries = copy.deepcopy(value["entries"])
    entries[-1]["payload"]["response_sha256"] = "0" * 64
    entries[-1]["digest"] = digest({k: v for k, v in entries[-1].items() if k != "digest"})
    with pytest.raises(ValueError, match="custody|consum|chain"):
        await implementation().prepare_final_outcome(value["run"], value["task"], entries)
    assert len(value["environment"].posts) == 2
