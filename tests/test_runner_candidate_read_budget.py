# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Candidate inspection owns its bounded thread until cooperative termination."""

import asyncio
import importlib
import threading

import pytest

from firefly_forge.runner_continuations import RunnerContinuations
from firefly_forge.runtime_roles import RuntimeRoles
from firefly_forge.store import Store


def budget():
    module = importlib.import_module("firefly_forge.runner_continuations")
    model = getattr(module, "_CandidateReadBudget", None)
    assert model is not None, "Candidate reads need an explicit resource budget"
    return model()


def manager(tmp_path):
    store = Store(tmp_path / "state" / "factory.db")
    return RunnerContinuations(store, RuntimeRoles(store), None)


async def test_source_hash_runs_outside_event_loop_and_reaps_cancelled_reader(tmp_path, monkeypatch):
    module = importlib.import_module("firefly_forge.runner_continuations")
    original = module.pack_workspace
    ready, release, finished = threading.Event(), threading.Event(), threading.Event()
    (tmp_path / "main.py").write_text("owned source")
    loop = asyncio.get_running_loop()
    reached = asyncio.Event()

    def blocked(*args, **kwargs):
        ready.set()
        loop.call_soon_threadsafe(reached.set)
        try:
            assert release.wait(5), "Owned test must release its source reader"
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(module, "pack_workspace", blocked)
    service = manager(tmp_path)
    task = asyncio.create_task(service._source_digest(tmp_path, 1024, budget()))
    try:
        await asyncio.wait_for(reached.wait(), 2)
        assert ready.is_set() and not task.done()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set() and not service._candidate_sources.locked()


async def test_source_byte_budget_refuses_without_changing_original_files(tmp_path):
    (tmp_path / "large.py").write_bytes(b"x" * 1025)
    original = (tmp_path / "large.py").read_bytes()
    with pytest.raises(ValueError):
        await manager(tmp_path)._source_digest(tmp_path, 1024, budget())
    assert (tmp_path / "large.py").read_bytes() == original


def test_source_deadline_and_checkpoint_work_budget_are_explicit(monkeypatch):
    module = importlib.import_module("firefly_forge.runner_continuations")
    current = budget()
    monkeypatch.setattr(module, "monotonic", lambda: current.deadline + 1)
    with pytest.raises(module._CandidateBudgetExceeded):
        current.checkpoint()
    monkeypatch.setattr(module, "monotonic", lambda: 0)
    current = budget()
    for _ in range(module.CANDIDATE_READ_CHECKPOINTS):
        current.checkpoint()
    with pytest.raises(module._CandidateBudgetExceeded):
        current.checkpoint()


async def test_busy_source_reader_refuses_without_queuing_another_thread(tmp_path, monkeypatch):
    module = importlib.import_module("firefly_forge.runner_continuations")
    service = manager(tmp_path)
    await service._candidate_sources.acquire()
    calls = []
    monkeypatch.setattr(module, "pack_workspace", lambda *args, **kwargs: calls.append(True))
    try:
        with pytest.raises(module._CandidateBudgetExceeded):
            await service._source_digest(tmp_path, 1024, budget())
    finally:
        service._candidate_sources.release()
    assert calls == []
