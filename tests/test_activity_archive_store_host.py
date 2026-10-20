# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""The actual Store.event writer owns one atomic index/content transaction."""

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_activity_archive import RUN, command_message

from firefly_forge.store import Store


@pytest.fixture
def event_store():
    store = object.__new__(Store)
    events, content, calls = [], [], []
    state = {"active": False, "count": 0, "bytes": 0, "fail_content": False, "reject": False}

    @asynccontextmanager
    async def unit():
        assert not state["active"]
        state["active"] = True
        before = copy.copy(events), copy.copy(content)
        try:
            yield None
        except BaseException:
            events[:], content[:] = before
            raise
        finally:
            state["active"] = False

    async def find_run(key, **kwargs):
        assert state["active"] and key == UUID(RUN) and kwargs.get("lock") is not None
        calls.append("lock")
        return SimpleNamespace(id=key)

    async def budget(key):
        assert state["active"] and key == UUID(RUN) and "lock" in calls
        return state["count"], state["bytes"]

    async def save_event(row):
        assert state["active"]
        events.append(row)

    async def save_content(row):
        assert state["active"]
        if state["fail_content"]:
            raise RuntimeError("Owned content transaction failure")
        content.append(row)

    async def admit(value):
        assert not state["active"]
        calls.append(value)
        if state["reject"]:
            raise ValueError("Configured credential in complete output")

    store._unit = unit
    store.run_records = SimpleNamespace(find_by_id=find_run)
    store.event_records = SimpleNamespace(save=save_event)
    store.event_content_records = SimpleNamespace(budget=budget, save=save_content)
    store.admit_credentials = admit
    store.reject_credentials = lambda value: None
    return store, events, content, calls, state


async def test_actual_writer_archives_the_entire_sanitized_message_and_small_summary(event_store):
    store, events, content, calls, _ = event_store
    message = command_message("x" * 70000)
    await store.event(RUN, "stdout", message)
    assert len(events) == len(content) == 1
    assert events[0].data["activity_summary"]["exit_code"] == 0
    assert content[0].raw_bytes == message.encode()
    assert content[0].event_id == events[0].id and content[0].run_id == UUID(RUN)
    assert message in calls


async def test_failed_blob_insert_rolls_back_its_activity_index(event_store):
    store, events, content, _, state = event_store
    state["fail_content"] = True
    with pytest.raises(RuntimeError, match="Owned content"):
        await store.event(RUN, "stdout", command_message("x" * 9000))
    assert events == [] and content == []


@pytest.mark.parametrize("budget", [{"count": 4096}, {"bytes": 67108864}])
async def test_run_archive_budget_refusal_is_explicit_without_losing_the_summary(event_store, budget):
    store, events, content, _, state = event_store
    state.update(budget)
    await store.event(RUN, "stdout", command_message("x" * 9000))
    assert content == []
    assert events[0].data["activity_content"]["status"] == "budget_exceeded"
    assert events[0].data["activity_summary"]["exit_code"] == 0


async def test_complete_message_credential_refusal_occurs_before_any_write(event_store):
    store, events, content, _, state = event_store
    state["reject"] = True
    with pytest.raises(ValueError):
        await store.event(RUN, "stdout", "prefix" * 2000 + "protected-tail")
    assert events == [] and content == []


async def test_non_provider_events_keep_the_legacy_payload_exactly(event_store):
    store, events, content, calls, _ = event_store
    await store.event(RUN, "implement", "x" * 9000)
    assert events[0].data["message"] == "x" * 8000
    assert set(events[0].data) == {"id", "stage", "message", "created_at"}
    assert content == [] and calls == []
