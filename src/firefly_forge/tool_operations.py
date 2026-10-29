# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Supervise one admitted tool through result commit without renewing its work cap."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

from firefly_forge.tool_operation_models import OwnedToolOperationV2

T = TypeVar("T")


class ToolWorkTimedOut(TimeoutError):
    pass


class ToolOwnershipLost(RuntimeError):
    pass


class OwnedToolSupervisor:
    def __init__(
        self,
        record: OwnedToolOperationV2,
        renew: Callable[[], Awaitable[None]],
        *,
        started_monotonic: float,
        interval: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.record, self.renew, self.started, self.clock = record, renew, started_monotonic, clock
        self.interval = record.owner_grace_seconds / 3 if interval is None else interval
        if self.interval <= 0 or self.interval >= record.owner_grace_seconds:
            raise ValueError("Tool ownership cadence must remain within its sealed grace")
        self.lock = asyncio.Lock()
        self.finished = False

    def _remaining_wait(self) -> float | None:
        chosen = self.record.work_duration.timeout_seconds
        if chosen is None:
            return None
        elapsed_ns = int((self.clock() - self.started) * 1_000_000_000)
        remaining_ns = chosen * 1_000_000_000 - elapsed_ns
        if remaining_ns <= 0:
            raise ToolWorkTimedOut("The original chosen tool work deadline expired")
        # Slicing timer waits avoids a platform timer limit without limiting work.
        return min(remaining_ns, 3600 * 1_000_000_000) / 1_000_000_000

    async def _renew(self) -> None:
        async with self.lock:
            if self.finished:
                return
            try:
                await self.renew()
            except (asyncio.CancelledError, ToolWorkTimedOut):
                raise
            except Exception as exc:
                raise ToolOwnershipLost("Current tool ownership is unavailable; inspect the retained intent") from exc

    async def _keepalive(self) -> None:
        while not self.finished:
            await asyncio.sleep(self.interval)
            await self._renew()

    @asynccontextmanager
    async def finalizing(self) -> AsyncIterator[None]:
        async with self.lock:
            self._remaining_wait()
            yield
            self.finished = True

    async def run(self, operation: Callable[[], Awaitable[T]]) -> T:
        self._remaining_wait()
        await self._renew()
        self._remaining_wait()

        async def execute() -> T:
            return await operation()

        work: asyncio.Task[T] = asyncio.create_task(execute())
        keeper = asyncio.create_task(self._keepalive())
        try:
            while True:
                completed, _ = await asyncio.wait(
                    (work, keeper), timeout=self._remaining_wait(), return_when=asyncio.FIRST_COMPLETED
                )
                if self.finished:
                    return await work
                if keeper in completed:
                    await keeper
                    raise ToolOwnershipLost("Tool ownership stopped before a native result was committed")
                if work in completed:
                    return await work
        finally:
            for task in (keeper, work):
                if not task.done():
                    task.cancel()
            await asyncio.gather(keeper, work, return_exceptions=True)
