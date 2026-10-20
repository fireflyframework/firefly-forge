# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Native scheduler shutdown must let an already-cancelled effect finish draining."""

import asyncio
from types import SimpleNamespace

import pytest

from firefly_forge.engine import Engine


async def test_shutdown_does_not_cancel_a_fence_loss_drain_twice(tmp_path):
    async def lost_ownership():
        raise RuntimeError("PostgreSQL execution ownership was lost")

    async def release_ownership():
        pass

    store = SimpleNamespace(
        path=tmp_path / "factory.db",
        runtime_roles=None,
        execution_stopping=False,
        assert_ownership=lost_ownership,
        release_ownership=release_ownership,
    )
    engine = Engine(store, tmp_path / "workspaces")
    engine.enforce_ownership = True
    engine.owner_lock.acquire(timeout=0)
    running, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    receipt = tmp_path / "drained-receipt"

    async def effect():
        running.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()
            receipt.write_text("interrupted after process drain\n")

    active = asyncio.create_task(effect())
    engine.active["fixture"] = active
    await running.wait()
    engine.poller = asyncio.create_task(engine._poll())
    await asyncio.wait_for(draining.wait(), 1)
    stopping = asyncio.create_task(engine.stop())
    try:
        # A second consumer detecting the same lost lease can start native shutdown
        # while the execution poller is still waiting for its first cancellation.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(engine.poller), 1)
        assert not active.done(), "Shutdown interrupted the effect's cancellation cleanup"
        assert not stopping.done() and engine.owner_lock.is_locked
        assert not receipt.exists()
        release.set()
        await asyncio.wait_for(stopping, 1)
        assert receipt.read_text() == "interrupted after process drain\n"
        assert active.cancelled() and not engine.owner_lock.is_locked
    finally:
        release.set()
        await asyncio.gather(stopping, active, return_exceptions=True)
        if engine.owner_lock.is_locked:
            engine.owner_lock.release()


async def test_shutdown_deadline_keeps_effect_cleanup_and_storage_fence(tmp_path):
    async def release_ownership():
        pass

    store = SimpleNamespace(
        path=tmp_path / "factory.db", runtime_roles=None, execution_stopping=False, release_ownership=release_ownership
    )
    engine = Engine(store, tmp_path / "workspaces")
    engine.enforce_ownership = True
    engine.owner_lock.acquire(timeout=0)
    running, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    receipt = tmp_path / "drained-receipt"

    async def effect():
        running.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()
            receipt.write_text("drained after shutdown deadline\n")

    active = asyncio.create_task(effect())
    engine.active["fixture"] = active
    await running.wait()
    stopping = asyncio.create_task(engine.stop())
    try:
        await asyncio.wait_for(draining.wait(), 1)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        assert not active.done(), "The shutdown deadline interrupted effect cleanup"
        assert engine.owner_lock.is_locked and not receipt.exists()
        release.set()
        await asyncio.gather(active, return_exceptions=True)
        await engine.stop()
        assert receipt.read_text() == "drained after shutdown deadline\n"
        assert not engine.owner_lock.is_locked
    finally:
        release.set()
        await asyncio.gather(stopping, active, return_exceptions=True)
        if engine.owner_lock.is_locked:
            engine.owner_lock.release()
