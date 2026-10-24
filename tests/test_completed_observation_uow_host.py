# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Strict session boundaries for archived completed-output observations."""

import copy
import hashlib
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import SimpleNamespace

import pytest
from pyfly.data.transaction.errors import IllegalTransactionStateError
from test_source_completed_output_host import output_fixture

from firefly_forge import source_result_readmission as module


def strict_store(run, contents, *, missing_payload=False):
    current = ContextVar("completed_observation_test_unit", default=None)
    queries, opened = [], []

    @asynccontextmanager
    async def unit():
        if current.get() is not None:
            yield current.get()
            return
        session = object()
        opened.append(session)
        token = current.set(session)
        try:
            yield session
        finally:
            current.reset(token)

    def require_session(kind):
        if current.get() is None:
            raise IllegalTransactionStateError("A controlled native repository needs its current unit")
        queries.append((kind, current.get()))

    async def events(run_id):
        require_session("events")
        assert run_id.hex == run["id"]
        return [SimpleNamespace(data=copy.deepcopy(row)) for row in run["events"]]

    async def payload(run_id, event_id, size, checksum):
        require_session("payload")
        assert run_id.hex == run["id"]
        if missing_payload:
            raise ValueError("Controlled archive payload is unavailable")
        raw = contents[event_id.hex]
        assert len(raw) == size and hashlib.sha256(raw).hexdigest() == checksum
        return raw

    store = SimpleNamespace(
        _unit=unit,
        event_records=SimpleNamespace(provider_budget_events=events),
        event_content_records=SimpleNamespace(payload=payload),
    )
    return store, current, queries, opened


@pytest.mark.parametrize("ambient", [False, True])
async def test_completed_archive_reads_share_a_unit_and_observation_preserves_the_callers_unit(monkeypatch, ambient):
    run, dispatch, contents = output_fixture()
    original = copy.deepcopy((run, dispatch, contents))
    store, current, queries, opened = strict_store(run, contents)
    observer = module.SourceCompletedOutputReadmissions(SimpleNamespace(recoveries=SimpleNamespace(store=store)))
    validate = module.completed_output_observation
    validator_sessions = []

    def observed(*args):
        validator_sessions.append(current.get())
        return validate(*args)

    monkeypatch.setattr(module, "completed_output_observation", observed)
    if ambient:
        async with store._unit() as caller:
            result = await observer._observation(run, dispatch)
            assert current.get() is caller
        assert validator_sessions == [caller]
    else:
        result = await observer._observation(run, dispatch)
        assert validator_sessions == [None]
    assert current.get() is None and len(opened) == 1
    assert [kind for kind, _ in queries] == ["events", "payload", "payload", "payload"]
    assert all(session is opened[0] for _, session in queries)
    assert result == validate(run, dispatch, contents)
    assert result["implementation_success"] is False and result["process_exit_attested"] is False
    assert (run, dispatch, contents) == original


async def test_archive_repository_failure_closes_owned_unit_without_observation(monkeypatch):
    run, dispatch, contents = output_fixture()
    original = copy.deepcopy((run, dispatch, contents))
    store, current, queries, opened = strict_store(run, contents, missing_payload=True)
    observer = module.SourceCompletedOutputReadmissions(SimpleNamespace(recoveries=SimpleNamespace(store=store)))

    def unexpected(*args):
        raise AssertionError("Failed archive reads cannot produce a completed observation")

    monkeypatch.setattr(module, "completed_output_observation", unexpected)
    with pytest.raises(ValueError, match="Controlled archive payload is unavailable"):
        await observer._observation(run, dispatch)
    assert current.get() is None and len(opened) == 1
    assert [kind for kind, _ in queries] == ["events", "payload"]
    assert (run, dispatch, contents) == original
