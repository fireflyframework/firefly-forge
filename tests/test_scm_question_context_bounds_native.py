# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Real PG and configured native question publication/answer with large irrelevant WAL."""

import json
from uuid import uuid4

import pytest
import test_native_question_publication_native as outbox
from scm_polling_native_fixtures import native_poll as native_poll


@pytest.mark.parametrize("native_poll", ["implementation"], indirect=True)
def test_native_unmentioned_answer_after_large_unrelated_tool_history(native_poll, tmp_path, monkeypatch):
    case = native_poll
    completed = outbox.completed_pending
    payload = "Public bounded fixture output. " * 20000
    assert len(json.dumps({"output": payload}).encode()) < 1_000_000
    assert len(json.dumps([{"output": payload}] * 8).encode()) > 4 * 1024 * 1024

    def large_history(*args, **kwargs):
        retained = completed(*args, **kwargs)

        async def append():
            async with case.store._unit():
                for _ in range(8):
                    await case.journal._append(
                        case.run["id"], "implement", uuid4().hex, "tool_receipt", {"output": payload}
                    )
            entries = await case.journal.entries(case.run["id"])
            assert len(json.dumps(entries).encode()) > 4 * 1024 * 1024
            assert case.journal.verify_chain(entries)

        case.http.portal.call(append)
        return retained

    monkeypatch.setattr(outbox, "completed_pending", large_history)
    # Preserve the exact existing linked completed-output -> publisher -> normal human answer journey,
    # including one native response and no resume, and add only unrelated public tool-output volume.
    outbox.test_native_new_question_publication_accepts_natural_answer_without_resuming(case, tmp_path, monkeypatch)
