# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Cleanup control budgets survive vendor cancellation suppression."""

import asyncio
from types import SimpleNamespace

import pytest

from firefly_forge import model_operation_client as module


@pytest.mark.parametrize("stalled_part", ["vendor", "http"])
@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_uncooperative_cleanup_returns_unknown_without_resetting_control_budget(
    monkeypatch, stalled_part, repeat_cancel
):
    monkeypatch.setattr(module, "MODEL_CLEANUP_TIMEOUT_SECONDS", 0.02)
    http = module.ModelOperationClient()
    original_close = http.aclose
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stalled():
        entered.set()
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
        finally:
            if stalled_part == "http":
                await original_close()
            finished.set()

    async def healthy():
        pass

    if stalled_part == "http":
        monkeypatch.setattr(http, "aclose", stalled)
    vendor = SimpleNamespace(close=stalled if stalled_part == "vendor" else healthy)

    class Provider:
        def __init__(self, client):
            self.client = client

    provider = module.owned_model_provider(Provider, http, client=vendor)

    async def work():
        async with provider:
            pass

    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if repeat_cancel:
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0.003)
        await asyncio.wait({task}, timeout=0.08)
        assert task.done(), "Vendor cancellation suppression exceeded the cleanup control budget"
        with pytest.raises(module.ModelOperationCleanupUnavailable):
            await task
        assert http.construction_refused, "Unconfirmed cleanup must disable subsequent sends"
        with pytest.raises(ValueError, match="refused"):
            await http.post("https://example.invalid/no-dispatch")
        if stalled_part == "vendor":
            assert http.is_closed, "Always attempt the separately owned HTTP transport close"
        else:
            assert not http.is_closed and not finished.is_set(), "Do not claim an undrained client is closed"
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.gather(task, return_exceptions=True)
        await original_close()
