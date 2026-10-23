# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Public native question/answer custody survives bounded unrelated journal volume."""

import json
from uuid import uuid4

from scm_polling_native_fixtures import native_poll as native_poll
from test_scm_natural_native import observe, prepare_worker


def test_native_unmentioned_answer_after_large_unrelated_tool_history(native_poll, monkeypatch):
    case = native_poll
    prepare_worker(case, monkeypatch)
    original = case.http.portal.call(case.store.get_run, case.run["id"])
    payload = {"output": "Public bounded fixture output. " * 20000}
    assert len(json.dumps(payload)) < 1_000_000

    async def append_history():
        async with case.store._unit():
            for _ in range(8):
                await case.journal._append(case.run["id"], "verify", uuid4().hex, "tool_receipt", payload)
        return await case.journal.entries(case.run["id"])

    retained = case.http.portal.call(append_history)
    assert len(json.dumps(retained).encode()) > 4 * 1024 * 1024
    assert case.journal.verify_chain(retained)
    delivery = observe(case, "Keep the existing result.")
    assert delivery["command"]["source_kind"] == "retained_question_reply"
    result = case.process(delivery)
    assert result["natural_turn"]["state"] == "answer_saved_pending_resume", result
    after = case.http.portal.call(case.store.get_run, case.run["id"])
    assert after["id"] == original["id"] and after["status"] == "paused"
    assert after["worker_request"]["status"] == "answered"
    for key in ("repair_budget", "implementation_rounds", "model_selection", "execution_config", "base_revision"):
        assert after.get(key) == original.get(key)
    entries = case.http.portal.call(case.journal.entries, case.run["id"])
    assert entries[: len(retained)] == retained
    assert [row["kind"] for row in entries[len(retained) :]] == ["worker_response"]
    assert entries[-1]["payload"]["answer"] == "Keep the existing result."
    assert case.state["natural_calls"] == 1
    assert case.process(delivery) == result
    assert case.http.portal.call(case.journal.entries, case.run["id"]) == entries
    assert case.state["natural_calls"] == 1
