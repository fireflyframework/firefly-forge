# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Offline actual-Journal controls for retained manual-resume counter shape."""

import copy
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_bridge_outcomes import MemoryStore, Rows
from test_scm_resume_cut import answered_cut  # noqa: F401
from test_scm_resume_service import resume_service  # noqa: F401

from firefly_forge.journal import Journal
from firefly_forge.project_access import ProjectAccess
from firefly_forge.scm_resume_cut import FROZEN_FIELDS, resume_cut


def real_journal(run, monkeypatch):
    store = MemoryStore()
    store.run.id = UUID(run["id"])
    store.run.status = run["status"]
    store.run.data = copy.deepcopy(run)
    task = SimpleNamespace(task_id="implement", status="paused", attempt=1, data={"known_worker_response": True})
    journal = Journal(store, tasks=Rows([task]), records=Rows([]))

    async def authorized(*args, **kwargs):
        pass

    store.assert_campaign_current = authorized
    monkeypatch.setattr(ProjectAccess, "authorize_execution", authorized)
    return journal, store


async def test_v2_resume_with_real_journal_preserves_missing_native_counter(resume_service, monkeypatch):  # noqa: F811
    service, row, scope, _ = resume_service
    del scope["run"]["retry_count"]
    cut = resume_cut(**scope)
    row.data["command"]["expected_cut_digest"] = cut["digest"]
    original = copy.deepcopy(scope["run"])
    journal, store = real_journal(original, monkeypatch)
    service.journal = journal
    result = await service.process(row.data["id"])
    assert result["status"] == "processed" and result["action_result"]["status"] == "queued"
    assert "retry_count" not in store.run.data
    assert {key: store.run.data.get(key) for key in FROZEN_FIELDS} == {key: original.get(key) for key in FROZEN_FIELDS}
    assert store.run.data["execution_actor"] == original["execution_actor"]
    assert len([entry for entry in journal.records.rows if entry.kind == "resume"]) == 1
    assert await service.process(row.data["id"]) == result
    assert len([entry for entry in journal.records.rows if entry.kind == "resume"]) == 1


@pytest.mark.parametrize("counter", [None, 0, 2], ids=["absent", "zero", "positive"])
@pytest.mark.parametrize("auto", [False, True], ids=["manual", "automatic"])
async def test_real_journal_counter_preserves_manual_shape_and_counts_automatic_retries(counter, auto, monkeypatch):
    run = {
        "id": UUID(int=1).hex,
        "status": "paused",
        "agent": "bridge",
        "pause": {"kind": "rate_limit", "automatic": True, "retry_at": 1},
    }
    if counter is not None:
        run["retry_count"] = counter
    journal, store = real_journal(run, monkeypatch)
    resumed = await journal.resume(run["id"], auto=auto)
    assert resumed["status"] == "queued"
    if auto:
        assert resumed["retry_count"] == (counter or 0) + 1
    elif counter is None:
        assert "retry_count" not in resumed
    else:
        assert resumed["retry_count"] == counter
    entries = [entry.data for entry in journal.records.rows]
    assert Journal.verify_chain(entries)
    assert entries[-1]["kind"] == "resume"
    assert entries[-1]["payload"]["automatic"] is auto
    assert store.run.status == "queued"
