# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fictional full-frontier argument artifacts preserve known effects without callbacks."""

import copy
import hashlib

import pytest
from test_bridge_inflight_cancel import RUN
from test_checkpoint_continuation_host import pending_after_write

from firefly_forge.agentic_tool_results import encoded
from firefly_forge.checkpoint_continuation_proof import pending_checkpoint
from firefly_forge.journal import Journal, digest

pytest_plugins = ["test_invalid_read_native_protocol_host"]


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
@pytest.mark.parametrize("raw_artifact", [False, True])
async def test_known_write_argument_artifacts_preserve_the_full_pending_frontier(
    continuity_case, monkeypatch, raw_artifact
):
    c = continuity_case
    _, entries = await pending_after_write(c, monkeypatch)
    run, task, _ = await c.continuity.recoveries._native(RUN)
    baseline = pending_checkpoint(run, task, entries)
    rows = copy.deepcopy(entries)
    intent = next(e for e in rows if e["kind"] == "agentic_tool_intent" and e["payload"]["tool"] == "write_file")
    for field in ["arguments", "raw_arguments"] if raw_artifact else ["arguments"]:
        value = intent["payload"].pop(field)
        raw = encoded(value)
        intent["payload"][field + "_artifact"] = {
            "version": 1,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "media_type": "application/json",
        }
        assert intent["payload"][field + "_sha256"] == digest({field: value})
    remapped = {}

    def references(value):
        if isinstance(value, dict):
            return {key: references(item) for key, item in value.items()}
        if isinstance(value, list):
            return [references(item) for item in value]
        return remapped.get(value, value) if isinstance(value, str) else value

    for sequence, original in enumerate(rows, 1):
        previous_digest = original["digest"]
        entry = references(original)
        entry["previous_hash"] = rows[sequence - 2]["digest"] if sequence > 1 else "0" * 64
        entry["digest"] = digest({key: value for key, value in entry.items() if key != "digest"})
        rows[sequence - 1] = entry
        remapped[previous_digest] = entry["digest"]
    assert Journal.verify_chain(rows)
    assert rows[-2]["payload"]["snapshot"] == entries[-2]["payload"]["snapshot"]
    assert rows[-1]["payload"]["response"] == entries[-1]["payload"]["response"]
    frontier = pending_checkpoint(run, task, rows)
    assert frontier["checkpoint"] == rows[-2] and frontier["response"] == rows[-1]
    assert frontier["closed_requests"] == baseline["closed_requests"]
    assert frontier["usage"] == baseline["usage"] and frontier["limits"] == baseline["limits"]
    assert frontier["state"] == baseline["state"]
    assert len(c.posts) == 3
    assert (c.workspace / "existing.py").read_text() == "VALUE = 2\n"
