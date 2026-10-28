# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Fictional host custody regression; no native database or actual provider."""

import copy

import pytest
from test_bridge_inflight_cancel import RUN
from test_checkpoint_continuation_host import pending_after_write

from firefly_forge.checkpoint_continuation_proof import pending_checkpoint
from firefly_forge.journal import Journal, digest

pytest_plugins = ["test_invalid_read_native_protocol_host"]


@pytest.mark.parametrize("continuity_case", [True], indirect=True)
@pytest.mark.parametrize("field", ["arguments", "raw_arguments"])
async def test_prior_known_write_arguments_must_match_immutable_model_call(continuity_case, monkeypatch, field):
    c = continuity_case
    _, entries = await pending_after_write(c, monkeypatch)
    run, task, _ = await c.continuity.recoveries._native(RUN)
    assert pending_checkpoint(run, task, entries)["checkpoint"] == entries[-2]
    rows = copy.deepcopy(entries)
    intent = next(e for e in rows if e["kind"] == "agentic_tool_intent" and e["payload"]["tool"] == "write_file")
    if field == "arguments":
        intent["payload"][field]["content"] = "VALUE = 999\n"
    else:
        intent["payload"][field] = '{"path":"existing.py","content":"VALUE = 999\\n"}'
    intent["payload"][field + "_sha256"] = digest({field: intent["payload"][field]})
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
    with pytest.raises(ValueError):
        pending_checkpoint(run, task, rows)
